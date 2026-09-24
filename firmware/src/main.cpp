// CRPMS bench rig — ESP32 Modbus server, TCP over WiFi or RTU over RS-485
//
// Two 12 V fans plus a switchable third on one supply, instrumented with an
// ACS712 current sensor, an MPU-6050 or MPU-6500 for vibration, two DS18B20 probes
// (motor hub and ambient). Published as Modbus unit id 1 -- over TCP by
// default, or over RS-485 through a MAX485 with the `rtu` build.
//
// The register map is in ../REGISTER_MAP.md and is the contract. Read it before
// changing anything here. Rig-specific settings are in rig_config.h; WiFi
// credentials in secrets.h (not committed).
//
// THE RULE THIS FIRMWARE EXISTS TO HONOUR. When a sensor read fails, the
// firmware says so distinctly: the status bit for that point is cleared AND the
// register holds a sentinel no sensor can produce. It does not return zero --
// zero amps and zero vibration are what a stopped fan reads -- it does not
// return the last good value, and it does not pass through the sensor's own
// error codes. The DS18B20 reports 85.0 degC after a power-on reset and
// -127.0 degC on a bus error, and 85 degC is a credible motor hub temperature.
//
// Libraries: eModbus (MIT), OneWire, DallasTemperature. The accelerometer is
// read through its registers directly (see "Accelerometer" below).

#include <Arduino.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Wire.h>
#include <esp_adc_cal.h>

#include "rig_config.h"

#if defined(CRPMS_MODBUS_RTU)
  #include "ModbusServerRTU.h"
#else
  #include <WiFi.h>
  #include "ModbusServerTCPasync.h"
  #if __has_include("secrets.h")
    #include "secrets.h"
  #else
    #error "copy src/secrets.h.example to src/secrets.h and set the WiFi credentials"
  #endif
#endif

static const uint8_t  MODBUS_UNIT_ID = 1;
static const uint16_t MODBUS_PORT    = 502;

static const uint32_t SCAN_INTERVAL_MS    = 250;
static const uint32_t DS18B20_INTERVAL_MS = 1000;   // conversion takes ~750 ms
static const uint32_t PROBE_LIST_INTERVAL_MS = 10000;
static const uint32_t RELAY_STAGGER_MS    = 500;
// After a relay opens, how long before the current sensor may be zeroed.
static const uint32_t RELAY_SETTLE_MS     = 300;

// ACS712 plausibility. The ESP32 ADC reads 0-3100 mV at 11 dB attenuation; an
// output pinned to either end is a disconnected or failed sensor, not a
// current. A zero (no-load output) far from Vcc/2 means the sensor, its supply
// or the calibration is wrong. And a load that is only fans cannot run
// backwards: a mean current clearly below zero means the zero is wrong.
// The rails are on the ADC's own reading; the zero window is on the corrected
// voltage (acsMv()).
static const float ACS_RAIL_LOW_MV   = 50.0f;
static const float ACS_RAIL_HIGH_MV  = 3050.0f;
static const float ACS_ZERO_MIN_MV   = 2300.0f;
static const float ACS_ZERO_MAX_MV   = 2700.0f;
static const float ACS_NEGATIVE_A    = -0.10f;
// Averaging. Each scan takes ACS_SAMPLES_PER_SCAN readings; the register holds
// the mean of the last ACS_AVERAGE_SCANS scans (one second at 250 ms). Chosen
// on the bench, 24 Sep 2026, to bring the fans-off noise inside +/-10 mA with
// WiFi running: 200 samples over 12 ms, as first written, wandered -69..+5 mA.
// A scan that fails a check empties the window, so a fault shows at once and
// good readings take a second to return.
static const int ACS_SAMPLES_PER_SCAN = 400;
static const int ACS_AVERAGE_SCANS    = 4;
static const float SUPPLY_ADC_MAX_MV = 3050.0f;
// The bottom of the range Espressif characterises at 11 dB. Below it the ADC
// cannot tell a small voltage from none: with the supply disconnected this
// board reads about 142 mV on the divider, which would otherwise be published
// as a supply of 0.7 V. A reading under the floor is "cannot measure", not a
// voltage.
static const float SUPPLY_ADC_MIN_MV = 150.0f;

// Register map (see REGISTER_MAP.md)
enum : uint16_t {
  IREG_CURRENT_MA   = 0,   // int16
  IREG_VIB_MMS_X100 = 1,   // uint16
  IREG_TEMP_HUB_X10 = 2,   // int16
  IREG_TEMP_AMB_X10 = 3,   // int16
  IREG_SUPPLY_MV    = 4,   // uint16
  IREG_STATUS       = 5,
  IREG_SCAN_COUNT   = 6,
  IREG_ACS_ZERO_MV  = 7,   // uint16, 0 until zeroed
  IREG_COUNT        = 8
};

enum : uint16_t {
  ST_HUB_OK        = 1 << 0,
  ST_AMBIENT_OK    = 1 << 1,
  ST_MPU_OK        = 1 << 2,
  ST_ACS_OK        = 1 << 3,
  ST_SUPPLY_OK     = 1 << 4,
  ST_BUS_OK        = 1 << 5,
  ST_ACS_ZEROED    = 1 << 6,
  ST_PROBES_CONFIG = 1 << 7,
  ST_SWITCH_FITTED = 1 << 8,   // RUN_SWITCH_FITTED in rig_config.h
  ST_RELAYS_FITTED = 1 << 9    // RELAYS_FITTED in rig_config.h
};

enum : uint16_t {
  COIL_RELAY_1 = 0,
  COIL_RELAY_2 = 1,
  COIL_REZERO  = 2,
  COIL_COUNT   = 3
};

// Sentinels: values no sensor on this rig can produce. The status bit is the
// signal; the sentinel exists so a master that ignores the status word still
// cannot mistake the register for a reading.
static const int16_t  INVALID_S16 = INT16_MIN;   // -32768
static const uint16_t INVALID_U16 = 0xFFFF;      // 65535

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

#if defined(CRPMS_MODBUS_RTU)
ModbusServerRTU modbusServer(2000, PIN_RS485_DE);
#else
ModbusServerTCPasync modbusServer;
#endif
OneWire oneWire(PIN_ONEWIRE);
DallasTemperature probes(&oneWire);

static uint16_t inputRegisters[IREG_COUNT] = {0};
static bool     discreteInputs[3] = {false, false, false};
static bool     relayOn[2] = {false, false};

static bool  probesConfigured = false;
static bool  mpuPresent = false;
static uint8_t mpuAddress = 0x68;
static const char *mpuName = "none";
static float acsZeroMv = 0.0f;
static bool  acsZeroed = false;
static uint32_t lastScan = 0, lastTempRequest = 0, lastProbeList = 0;
static uint32_t lastRelayOn = 0, lastRelayOff = 0;
static bool tempConversionPending = false;
static volatile bool rezeroRequested = false;

static inline void setStatus(uint16_t bit, bool ok) {
  if (ok) inputRegisters[IREG_STATUS] |= bit;
  else    inputRegisters[IREG_STATUS] &= ~bit;
}

static inline uint16_t asRegister(int16_t v) { return (uint16_t)v; }

// ---------------------------------------------------------------------------
// Relays
// ---------------------------------------------------------------------------

static void relayWrite(int pin, bool energise) {
  // Active-low: energised is LOW.
  digitalWrite(pin, (energise != RELAY_ACTIVE_LOW) ? HIGH : LOW);
}

static void setRelay(uint16_t index, bool energise) {
  relayOn[index] = energise;
  relayWrite(index == 0 ? PIN_RELAY_1 : PIN_RELAY_2, energise);
  uint32_t now = millis();
  if (energise) lastRelayOn = now; else lastRelayOff = now;
}

static float lastSupplyAdcMv = 0.0f;

// The supply divider, averaged, and remembered for the Modbus handlers (which
// run in another task and must not touch the ADC themselves).
static float readSupplyAdcMv() {
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) sum += analogReadMilliVolts(PIN_SUPPLY_ADC);
  lastSupplyAdcMv = sum / 16.0f;
  return lastSupplyAdcMv;
}

// Whether the current sensor can be zeroed now, i.e. no load current flows.
//
// With relays fitted, that is both relays de-energised and settled. Without
// them the fans are wired straight to the 12 V supply, so the only evidence
// the load is off is the supply itself: the zero is allowed only while the
// supply divider reads below what the ADC can measure. The operating rule
// until the relays arrive is USB before 12 V; this is what enforces it.
static bool loadIsOff() {
  if (!RELAYS_FITTED) return lastSupplyAdcMv < SUPPLY_ADC_MIN_MV;
  return !relayOn[0] && !relayOn[1] && millis() - lastRelayOff >= RELAY_SETTLE_MS;
}

// The ACS712 output as a voltage: the ESP32's calibrated reading, corrected by
// the offset measured on this bench against a multimeter (rig_config.h).
static inline float acsMv() {
  return analogReadMilliVolts(PIN_ACS712) + ACS712_ADC_OFFSET_MV;
}

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------

// Zero the current sensor. Only with every relay de-energised and settled: a
// zero taken with the fans running would offset every current reading for the
// life of the boot. Returns whether the zero is plausible.
static bool zeroCurrentSensor() {
  if (!RELAYS_FITTED) readSupplyAdcMv();
  if (!loadIsOff()) {
    // Refused, not taken: a zero with the fans running would publish every
    // later current as a Good reading about 0.46 A wrong.
    acsZeroed = false;
    setStatus(ST_ACS_ZEROED, false);
    if (!RELAYS_FITTED) {
      Serial.printf("WARNING: ACS712 NOT zeroed. The supply reads %.0f mV at the "
                    "divider, so 12 V is connected, and with no relays fitted the "
                    "fans are running: a zero now would include their current. "
                    "Current reports invalid. Disconnect 12 V and reset the ESP32 "
                    "(USB first, then 12 V).\n", lastSupplyAdcMv);
    } else {
      Serial.println("ACS712 zero refused: a relay is energised or not yet settled");
    }
    return false;
  }
  double sum = 0;
  for (int i = 0; i < 500; i++) { sum += acsMv(); delay(1); }
  float zero = (float)(sum / 500.0);
  bool plausible = zero >= ACS_ZERO_MIN_MV && zero <= ACS_ZERO_MAX_MV;
  acsZeroMv = zero;
  acsZeroed = plausible;
  inputRegisters[IREG_ACS_ZERO_MV] = (uint16_t)lroundf(zero);
  setStatus(ST_ACS_ZEROED, plausible);
  Serial.printf("ACS712 zero: %.1f mV after the %+.0f mV bench correction (%s)\n",
                zero, (float)ACS712_ADC_OFFSET_MV,
                plausible ? "plausible" : "IMPLAUSIBLE - current will be invalid");
  return plausible;
}

// Mean current over ~12 ms. The load is DC fans, so the mean is the current;
// it is signed so that a wrong zero shows as a negative current and is caught,
// where an RMS would have hidden the sign and reported it as load.
static float acsWindow[ACS_AVERAGE_SCANS];
static int acsWindowCount = 0, acsWindowNext = 0;

static float readCurrentAmpsOnce(bool &ok) {
  const int samples = ACS_SAMPLES_PER_SCAN;
  double sum = 0.0;
  int atRail = 0;
  for (int i = 0; i < samples; i++) {
    // The rails are where the ADC itself stops, so they are judged on the
    // uncorrected reading; the sum uses the corrected one.
    float raw = analogReadMilliVolts(PIN_ACS712);
    if (raw <= ACS_RAIL_LOW_MV || raw >= ACS_RAIL_HIGH_MV) atRail++;
    sum += raw + ACS712_ADC_OFFSET_MV;
    delayMicroseconds(50);
  }
  float amps = ACS712_SIGN * ((float)(sum / samples) - acsZeroMv) / ACS712_MV_PER_A;
  ok = acsZeroed && atRail < samples / 10;
  return amps;
}

// The published current: the mean of the last ACS_AVERAGE_SCANS good scans.
// Invalid until the window is full, and emptied by any scan that fails, so no
// average ever spans a fault. The negative-current check is on the average.
static float readCurrentAmps(bool &ok) {
  bool scanOk = false;
  float amps = readCurrentAmpsOnce(scanOk);
  if (!scanOk) {
    acsWindowCount = 0;
    ok = false;
    return amps;
  }
  acsWindow[acsWindowNext] = amps;
  acsWindowNext = (acsWindowNext + 1) % ACS_AVERAGE_SCANS;
  if (acsWindowCount < ACS_AVERAGE_SCANS) acsWindowCount++;
  float sum = 0.0f;
  for (int i = 0; i < acsWindowCount; i++) sum += acsWindow[i];
  float mean = sum / acsWindowCount;
  ok = acsWindowCount == ACS_AVERAGE_SCANS && mean > ACS_NEGATIVE_A;
  return mean;
}

// ---------------------------------------------------------------------------
// Accelerometer
// ---------------------------------------------------------------------------
//
// The module on this bench is sold as an MPU-6050 but identifies itself as an
// MPU-6500 (WHO_AM_I 0x70, read on 24 Sep 2026), and the Adafruit MPU6050
// driver refuses anything that does not answer 0x68. The accelerometer
// registers used here are the same on both parts, so both are read directly:
// wake (PWR_MGMT_1), full scale +/-4 g (ACCEL_CONFIG), six bytes from
// ACCEL_XOUT_H. A device with any other identity is not accepted: its
// registers are not known to mean this.

static const uint8_t MPU_REG_PWR_MGMT_1   = 0x6B;
static const uint8_t MPU_REG_ACCEL_CONFIG = 0x1C;
static const uint8_t MPU_REG_ACCEL_XOUT_H = 0x3B;
static const uint8_t MPU_REG_WHO_AM_I     = 0x75;
static const float   MPU_LSB_PER_G        = 8192.0f;   // at +/-4 g
static const float   STANDARD_GRAVITY     = 9.80665f;

static bool mpuWrite(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(mpuAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static int mpuReadByte(uint8_t address, uint8_t reg) {
  Wire.beginTransmission(address);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -1;
  if (Wire.requestFrom(address, (uint8_t)1) != 1) return -1;
  return Wire.read();
}

static bool mpuBegin() {
  for (uint8_t address : {(uint8_t)0x68, (uint8_t)0x69}) {
    int who = mpuReadByte(address, MPU_REG_WHO_AM_I);
    if (who < 0) continue;
    const char *name = who == 0x68 ? "MPU-6050" : who == 0x70 ? "MPU-6500" : nullptr;
    if (!name) {
      Serial.printf("I2C 0x%02X answers WHO_AM_I 0x%02X: not an MPU-6050 or "
                    "MPU-6500, so not used\n", address, who);
      continue;
    }
    mpuAddress = address;
    mpuName = name;
    bool ok = mpuWrite(MPU_REG_PWR_MGMT_1, 0x01)       // awake, PLL clock
              && mpuWrite(MPU_REG_ACCEL_CONFIG, 0x08);  // +/-4 g
    delay(50);
    Serial.printf("accelerometer: %s at 0x%02X (WHO_AM_I 0x%02X)%s\n", name,
                  address, who, ok ? "" : ", but it did not accept its setup");
    return ok;
  }
  Serial.println("accelerometer: none found at 0x68 or 0x69");
  return false;
}

static bool mpuReadAccel(float &x, float &y, float &z) {
  Wire.beginTransmission(mpuAddress);
  Wire.write(MPU_REG_ACCEL_XOUT_H);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(mpuAddress, (uint8_t)6) != 6) return false;
  int16_t raw[3];
  for (int i = 0; i < 3; i++) {
    uint8_t hi = Wire.read(), lo = Wire.read();
    raw[i] = (int16_t)((hi << 8) | lo);
  }
  x = raw[0] / MPU_LSB_PER_G * STANDARD_GRAVITY;
  y = raw[1] / MPU_LSB_PER_G * STANDARD_GRAVITY;
  z = raw[2] / MPU_LSB_PER_G * STANDARD_GRAVITY;
  return true;
}

// Vibration as the RMS of the varying part of the acceleration, expressed as
// an equivalent velocity at the fan's running frequency. An approximation, and
// documented as one rather than presented as an ISO 10816 velocity: a proper
// velocity figure needs integration of a calibrated accelerometer signal, and
// this is a bench fan with a hobby accelerometer.
//
// The varying part is taken about the window's own mean, not about standard
// gravity. Subtracting 9.80665 m/s^2 left any scale error in the sensor as a
// constant, and the RMS reported that constant as vibration: 1.02 mm/s from
// this MPU-6500 lying still on the bench, fans off, 24 Sep 2026.
static float readVibrationMmS(bool &ok) {
  ok = false;
  if (!mpuPresent) return 0.0f;
  const int samples = 64;
  float magnitude[samples];
  double mean = 0.0;
  for (int i = 0; i < samples; i++) {
    float x, y, z;
    if (!mpuReadAccel(x, y, z)) return 0.0f;
    magnitude[i] = sqrtf(x * x + y * y + z * z);
    mean += magnitude[i];
    delayMicroseconds(500);
  }
  mean /= samples;
  double sum = 0.0;
  for (int i = 0; i < samples; i++) {
    double ac = magnitude[i] - mean;
    sum += ac * ac;
  }
  ok = true;
  float accelRms = sqrt(sum / samples);              // m/s^2
  const float fanHz = 40.0f;                         // nominal running speed
  return (accelRms / (2.0f * PI * fanHz)) * 1000.0f; // mm/s
}

static bool addressSet(const uint8_t *address) {
  for (int i = 0; i < 8; i++) if (address[i]) return true;
  return false;
}

// Read one probe BY ADDRESS. Never by bus position.
static int16_t readProbe(const uint8_t *address, bool &ok) {
  ok = false;
  if (!probesConfigured) return INVALID_S16;
  float c = probes.getTempC(address);
  // DEVICE_DISCONNECTED_C is -127: no answer from that address. 85.0 exactly
  // is the power-on-reset register value -- a conversion that never happened.
  // A genuine 85.0000 reading is indistinguishable from it and is sacrificed:
  // it reports invalid rather than risk passing a reset off as a reading.
  // Outside -55..125 is outside the DS18B20's range altogether.
  if (c == DEVICE_DISCONNECTED_C || c == 85.0f || c < -55.0f || c > 125.0f) {
    return INVALID_S16;
  }
  ok = true;
  return (int16_t)lroundf(c * 10.0f);
}

// A probe that has just come back -- plugged in, or at boot -- is not trusted
// on its first reading. Found on the bench, 24 Sep 2026: the hub probe, plugged
// back in after the Stage 10 unplug test, returned 99.3 degC with a valid CRC,
// then 29.3 degC a second later; the 99.3 was published and archived as Good.
// A reading that has not been confirmed is reported invalid, and a probe is
// trusted again only when two successive conversions agree within
// PROBE_CONFIRM_X10 (tenths of a degree) -- one second apart, far more than any
// real change of a fan hub or the air.
static const int16_t PROBE_CONFIRM_X10 = 10;

struct ProbeState {
  bool settled = false;        // last reading was confirmed
  bool haveCandidate = false;  // an unconfirmed reading is waiting
  int16_t candidate = 0;
};
static ProbeState hubState, ambientState;

static int16_t readProbeConfirmed(const uint8_t *address, ProbeState &st, bool &ok) {
  bool readOk = false;
  int16_t v = readProbe(address, readOk);
  ok = false;
  if (!readOk) {
    st.settled = false;
    st.haveCandidate = false;
    return INVALID_S16;
  }
  if (st.settled) {
    ok = true;
    return v;
  }
  if (st.haveCandidate && abs(v - st.candidate) <= PROBE_CONFIRM_X10) {
    st.settled = true;
    st.haveCandidate = false;
    ok = true;
    return v;
  }
  st.candidate = v;            // first reading back, or it disagreed: wait
  st.haveCandidate = true;
  return INVALID_S16;
}

static void listProbes() {
  DeviceAddress found;
  oneWire.reset_search();
  int count = 0;
  while (oneWire.search(found)) {
    if (OneWire::crc8(found, 7) != found[7]) continue;
    // The reading beside each address is what lets a person tell the probes
    // apart: warm one in your fingers and watch which address rises. It is
    // printed here only; the registers read probes by configured address.
    float c = probes.getTempC(found);
    const char *role = memcmp(found, HUB_PROBE_ADDRESS, 8) == 0 ? " = HUB"
                     : memcmp(found, AMBIENT_PROBE_ADDRESS, 8) == 0 ? " = AMBIENT"
                     : "";
    Serial.printf("DS18B20 on bus: {0x%02X,0x%02X,0x%02X,0x%02X,0x%02X,0x%02X,0x%02X,0x%02X}"
                  "  %s%s\n",
                  found[0], found[1], found[2], found[3],
                  found[4], found[5], found[6], found[7],
                  (c == DEVICE_DISCONNECTED_C || c == 85.0f) ? "no reading yet"
                      : String(c, 2).c_str(),
                  role);
    count++;
  }
  Serial.printf("DS18B20 probes found: %d\n", count);
  setStatus(ST_BUS_OK, count > 0);
  if (!probesConfigured) {
    Serial.println("probe addresses not configured in rig_config.h: "
                   "both temperatures report invalid");
  }
}

// ---------------------------------------------------------------------------
// Modbus handlers
// ---------------------------------------------------------------------------

ModbusMessage onReadInputRegisters(ModbusMessage request) {
  uint16_t address = 0, words = 0;
  request.get(2, address);
  request.get(4, words);
  if (words == 0 || words > 125 || address + words > IREG_COUNT) {
    return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                         ILLEGAL_DATA_ADDRESS);
  }
  ModbusMessage response;
  response.add(request.getServerID(), request.getFunctionCode(),
               (uint8_t)(words * 2));
  for (uint16_t i = 0; i < words; i++) response.add(inputRegisters[address + i]);
  return response;
}

ModbusMessage onReadDiscreteInputs(ModbusMessage request) {
  uint16_t address = 0, bits = 0;
  request.get(2, address);
  request.get(4, bits);
  if (bits == 0 || address + bits > 3) {
    return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                         ILLEGAL_DATA_ADDRESS);
  }
  uint8_t packed = 0;
  for (uint16_t i = 0; i < bits; i++)
    if (discreteInputs[address + i]) packed |= (1 << i);
  ModbusMessage response;
  response.add(request.getServerID(), request.getFunctionCode(), (uint8_t)1,
               packed);
  return response;
}

// Coils are the rig's own control surface -- relay commands and a re-zero
// request, from the rig's buttons or a bench tool. The CRPMS acquisition path
// never writes them: the bridge reads, and the collector has no write method.
ModbusMessage onWriteCoil(ModbusMessage request) {
  uint16_t address = 0, value = 0;
  request.get(2, address);
  request.get(4, value);
  if (address >= COIL_COUNT) {
    return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                         ILLEGAL_DATA_ADDRESS);
  }
  if (value != 0xFF00 && value != 0x0000) {
    return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                         ILLEGAL_DATA_VALUE);
  }
  bool on = (value == 0xFF00);
  uint32_t now = millis();

  if (address == COIL_REZERO) {
    // A re-zero with any load energised, or not yet settled, would calibrate
    // the load into the zero. Refused, and the master is told why it may retry.
    if (on) {
      if (!loadIsOff()) {
        return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                             SERVER_DEVICE_BUSY);
      }
      rezeroRequested = true;          // done in loop(), not in the handler
    }
  } else {
    // No relay to command: refused rather than acknowledged and ignored.
    if (!RELAYS_FITTED) {
      return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                           ILLEGAL_DATA_ADDRESS);
    }
    // Fan starts are staggered: three fans starting together draw about 1.2 A
    // against a 1 A supply.
    if (on && !relayOn[address] && now - lastRelayOn < RELAY_STAGGER_MS) {
      return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                           SERVER_DEVICE_BUSY);
    }
    setRelay(address, on);
  }
  ModbusMessage response;
  response.add(request.getServerID(), request.getFunctionCode(), address, value);
  return response;
}

// ---------------------------------------------------------------------------
// Serial diagnostics, for the bench. Read-only: printed from what the scan
// already put in the registers, so the serial monitor shows exactly what a
// Modbus master would be served.
// ---------------------------------------------------------------------------

static void printDiagnostics() {
  const uint16_t st = inputRegisters[IREG_STATUS];
  const int16_t ma = (int16_t)inputRegisters[IREG_CURRENT_MA];
  const uint16_t vib = inputRegisters[IREG_VIB_MMS_X100];
  const uint16_t supply = inputRegisters[IREG_SUPPLY_MV];
  Serial.printf("status 0x%03X | accelerometer %s | ACS712 zero %.1f mV (%s) | current ",
                st, (st & ST_MPU_OK) ? mpuName : "NOT read",
                acsZeroMv, acsZeroed ? "zeroed" : "NOT zeroed");
  if (st & ST_ACS_OK) Serial.printf("%.3f A", ma / 1000.0f);
  else Serial.print("invalid");
  Serial.print(" | vibration ");
  if (st & ST_MPU_OK) Serial.printf("%.2f mm/s", vib / 100.0f);
  else Serial.print("invalid");
  Serial.printf(" | supply ADC %.0f mV -> ", lastSupplyAdcMv);
  if (st & ST_SUPPLY_OK) Serial.printf("%.2f V", supply / 1000.0f);
  else Serial.print(lastSupplyAdcMv < SUPPLY_ADC_MIN_MV
                    ? "cannot measure (below the ADC's range)"
                    : "cannot measure (ADC saturated)");
  if (RUN_SWITCH_FITTED) Serial.printf(" | run switch %s", discreteInputs[0] ? "closed" : "open");
  else Serial.print(" | run switch not fitted");
  if (!RELAYS_FITTED) Serial.print(" | relays not fitted");
  Serial.println();
}

// ---------------------------------------------------------------------------
// Setup and loop
// ---------------------------------------------------------------------------

void setup() {
  // Relays first, before anything else can take time: de-energised. The level
  // is written before the pin becomes an output as well as after, so that the
  // output starts at the de-energised level. (Between reset and here the pins
  // are inputs; the modules' own pull-ups hold an active-low relay off.) Both
  // are to be confirmed on the bench: no relay should click at power-on or
  // reset.
  for (int pin : {PIN_RELAY_1, PIN_RELAY_2}) {
    relayWrite(pin, false);
    pinMode(pin, RELAY_OPEN_DRAIN ? OUTPUT_OPEN_DRAIN : OUTPUT);
    relayWrite(pin, false);
  }
  lastRelayOff = millis();

  Serial.begin(115200);
  pinMode(PIN_RUN_SWITCH, INPUT_PULLUP);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_ACS712, ADC_11db);
  analogSetPinAttenuation(PIN_SUPPLY_ADC, ADC_11db);

  for (uint16_t i = 0; i < IREG_COUNT; i++) inputRegisters[i] = 0;
  inputRegisters[IREG_CURRENT_MA]   = asRegister(INVALID_S16);
  inputRegisters[IREG_VIB_MMS_X100] = INVALID_U16;
  inputRegisters[IREG_TEMP_HUB_X10] = asRegister(INVALID_S16);
  inputRegisters[IREG_TEMP_AMB_X10] = asRegister(INVALID_S16);
  inputRegisters[IREG_SUPPLY_MV]    = INVALID_U16;

  setStatus(ST_SWITCH_FITTED, RUN_SWITCH_FITTED);
  setStatus(ST_RELAYS_FITTED, RELAYS_FITTED);
  {
    const char *cal =
        esp_adc_cal_check_efuse(ESP_ADC_CAL_VAL_EFUSE_TP) == ESP_OK ? "eFuse two-point"
      : esp_adc_cal_check_efuse(ESP_ADC_CAL_VAL_EFUSE_VREF) == ESP_OK ? "eFuse Vref"
      : "default Vref (this chip carries no ADC calibration)";
    Serial.printf("\nCRPMS bench rig. ADC calibration: %s. Relays %s, run switch %s.\n",
                  cal, RELAYS_FITTED ? "fitted" : "NOT fitted",
                  RUN_SWITCH_FITTED ? "fitted" : "NOT fitted");
  }

  probesConfigured = addressSet(HUB_PROBE_ADDRESS) &&
                     addressSet(AMBIENT_PROBE_ADDRESS);
  setStatus(ST_PROBES_CONFIG, probesConfigured);
  probes.begin();
  probes.setResolution(12);
  probes.setWaitForConversion(false);          // asynchronous: 750 ms at 12-bit
  listProbes();

  Wire.begin();
  // Which I2C addresses answer, printed once at boot, so "not detected" can
  // be told apart from "nothing on the bus".
  int i2cFound = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("I2C device at 0x%02X\n", addr);
      i2cFound++;
    }
  }
  if (i2cFound == 0) Serial.println("I2C: no device answers on SDA 21 / SCL 22");
  mpuPresent = mpuBegin();
  setStatus(ST_MPU_OK, mpuPresent);

  modbusServer.registerWorker(MODBUS_UNIT_ID, READ_INPUT_REGISTER,
                              &onReadInputRegisters);
  modbusServer.registerWorker(MODBUS_UNIT_ID, READ_DISCR_INPUT,
                              &onReadDiscreteInputs);
  modbusServer.registerWorker(MODBUS_UNIT_ID, WRITE_COIL, &onWriteCoil);

#if defined(CRPMS_MODBUS_RTU)
  RTUutils::prepareHardwareSerial(Serial2);
  Serial2.begin(RS485_BAUD, SERIAL_8N1, PIN_RS485_RX, PIN_RS485_TX);
  modbusServer.begin(Serial2);
  Serial.printf("rig on RS-485, %lu baud 8N1, unit %u\n",
                (unsigned long)RS485_BAUD, MODBUS_UNIT_ID);
  delay(RELAY_SETTLE_MS);
  zeroCurrentSensor();
#else
  // Why a connection fails is printed, not left as a row of dots: the
  // driver's disconnect reason, and once, whether the configured network is
  // visible at all (the ESP32 sees 2.4 GHz only). The password is never printed.
  static volatile uint8_t lastDisconnectReason = 0;
  WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t info) {
    lastDisconnectReason = info.wifi_sta_disconnected.reason;
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t waitStart = millis(), lastReport = millis();
  bool scanned = false;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (millis() - lastReport < 15000) continue;
    lastReport = millis();
    uint8_t r = lastDisconnectReason;
    const char *meaning =
        r == WIFI_REASON_NO_AP_FOUND ? "network not found (wrong name, or 5 GHz only)"
      : r == WIFI_REASON_AUTH_FAIL || r == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT ||
        r == WIFI_REASON_HANDSHAKE_TIMEOUT ? "authentication failed (password?)"
      : r == WIFI_REASON_ASSOC_FAIL ? "association refused by the router"
      : r == 0 ? "no disconnect reported yet" : "see esp_wifi_types.h";
    Serial.printf("\nWiFi not connected after %lu s: status %d, last reason %u -- %s\n",
                  (unsigned long)((millis() - waitStart) / 1000), (int)WiFi.status(),
                  r, meaning);
    if (!scanned) {
      scanned = true;
      // A scan while the station is still trying to join fails (-2); stop
      // trying, scan, then try again.
      WiFi.disconnect();
      delay(200);
      int n = WiFi.scanNetworks();
      if (n < 0) Serial.printf("WiFi scan failed (%d)\n", n);
      int exact = -1, loose = -1;
      String want = String(WIFI_SSID);
      for (int i = 0; i < n; i++) {
        if (WiFi.SSID(i) == want) exact = i;
        else if (WiFi.SSID(i).equalsIgnoreCase(want) ||
                 WiFi.SSID(i).indexOf(want) >= 0 || want.indexOf(WiFi.SSID(i)) >= 0) loose = i;
      }
      if (exact >= 0)
        Serial.printf("the configured network is visible: channel %d, %d dBm, %s\n",
                      WiFi.channel(exact), WiFi.RSSI(exact),
                      WiFi.encryptionType(exact) == WIFI_AUTH_WPA2_ENTERPRISE
                          ? "WPA2-Enterprise (not supported by this firmware)" : "personal security");
      else if (loose >= 0)
        Serial.printf("the configured network name is NOT visible exactly, but \"%s\" is "
                      "close: check case and spaces in secrets.h\n", WiFi.SSID(loose).c_str());
      else if (n >= 0)
        Serial.printf("the configured network (%u characters) is NOT visible among "
                      "%d 2.4 GHz networks (the ESP32 cannot see 5 GHz)\n",
                      (unsigned)want.length(), n);
      WiFi.scanDelete();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
  }
  Serial.printf("\nrig at %s:%u, unit %u\n", WiFi.localIP().toString().c_str(),
                MODBUS_PORT, MODBUS_UNIT_ID);
  // Radio power saving off: in modem-sleep the radio wakes in bursts, and the
  // bursts moved the no-load current by tens of mA (and delayed ARP replies,
  // so the rig was intermittently unreachable). A steady radio load is a
  // steady offset, which the zero below then includes.
  WiFi.setSleep(false);
  delay(500);
  // Zero the current sensor now, under the conditions it will read in: with
  // WiFi up. Only with the load off -- relays open, or with no relays, 12 V not
  // yet connected (zeroCurrentSensor() checks). A zero outside plausibility
  // leaves the current invalid until a good re-zero (coil 2) rather than
  // publishing offset readings.
  delay(RELAY_SETTLE_MS);
  zeroCurrentSensor();
  modbusServer.start(MODBUS_PORT, 4, 0);
#endif
}

void loop() {
  uint32_t now = millis();

  if (rezeroRequested) {
    rezeroRequested = false;
    zeroCurrentSensor();
  }

  if (now - lastProbeList >= PROBE_LIST_INTERVAL_MS) {
    lastProbeList = now;
    listProbes();
    printDiagnostics();
  }

  if (now - lastTempRequest >= DS18B20_INTERVAL_MS) {
    lastTempRequest = now;
    if (tempConversionPending) {
      bool hubOk = false, ambientOk = false;
      inputRegisters[IREG_TEMP_HUB_X10] =
          asRegister(readProbeConfirmed(HUB_PROBE_ADDRESS, hubState, hubOk));
      inputRegisters[IREG_TEMP_AMB_X10] =
          asRegister(readProbeConfirmed(AMBIENT_PROBE_ADDRESS, ambientState, ambientOk));
      setStatus(ST_HUB_OK, hubOk);
      setStatus(ST_AMBIENT_OK, ambientOk);
    }
    // A broadcast conversion request; each probe is then read by its address,
    // so a probe unplugged and plugged back is picked up without a reboot.
    probes.requestTemperatures();
    tempConversionPending = true;
  }

  if (now - lastScan < SCAN_INTERVAL_MS) return;
  lastScan = now;

  bool acsOk = false;
  float amps = readCurrentAmps(acsOk);
  inputRegisters[IREG_CURRENT_MA] = acsOk
      ? asRegister((int16_t)constrain(lroundf(amps * 1000.0f), -32767, 32767))
      : asRegister(INVALID_S16);
  setStatus(ST_ACS_OK, acsOk);

  bool vibOk = false;
  float vib = readVibrationMmS(vibOk);
  inputRegisters[IREG_VIB_MMS_X100] = vibOk
      ? (uint16_t)constrain(lroundf(vib * 100.0f), 0, 65534)
      : INVALID_U16;
  setStatus(ST_MPU_OK, vibOk);

  // The supply voltage is a MEASUREMENT whatever it reads: 9.8 V is exactly
  // the reading worth having during a brown-out. Only an ADC pinned at its
  // ceiling is invalid, because then the true voltage is unknown -- and so is
  // a reading under the floor, where the ADC cannot tell small from none.
  // Whether the voltage is acceptable is the monitoring system's judgement (a
  // range rule on RIG_SUPPLY_V), not the sensor's.
  float adcMv = readSupplyAdcMv();
  bool supplyOk = adcMv >= SUPPLY_ADC_MIN_MV && adcMv < SUPPLY_ADC_MAX_MV;
  inputRegisters[IREG_SUPPLY_MV] = supplyOk
      ? (uint16_t)constrain(lroundf(adcMv * SUPPLY_DIVIDER), 0, 65534)
      : INVALID_U16;
  setStatus(ST_SUPPLY_OK, supplyOk);

  // Not fitted: the pin floats to its pull-up and would read "stopped". The
  // bit is still served (false), and status bit 8 says it means nothing.
  discreteInputs[0] = RUN_SWITCH_FITTED && digitalRead(PIN_RUN_SWITCH) == LOW;
  discreteInputs[1] = relayOn[0];
  discreteInputs[2] = relayOn[1];

  inputRegisters[IREG_SCAN_COUNT]++;
}
