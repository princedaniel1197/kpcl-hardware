// CRPMS bench rig — ESP32 Modbus server, TCP over WiFi or RTU over RS-485
//
// Two 12 V fans plus a switchable third on one supply, instrumented with an
// ACS712 current sensor, an MPU-6050 for vibration, and two DS18B20 probes
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
// Libraries: eModbus (MIT), OneWire, DallasTemperature, Adafruit MPU6050.

#include <Arduino.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>

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
static const float ACS_RAIL_LOW_MV   = 50.0f;
static const float ACS_RAIL_HIGH_MV  = 3050.0f;
static const float ACS_ZERO_MIN_MV   = 2300.0f;
static const float ACS_ZERO_MAX_MV   = 2700.0f;
static const float ACS_NEGATIVE_A    = -0.10f;
static const float SUPPLY_ADC_MAX_MV = 3050.0f;

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
  ST_PROBES_CONFIG = 1 << 7
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
Adafruit_MPU6050 mpu;

static uint16_t inputRegisters[IREG_COUNT] = {0};
static bool     discreteInputs[3] = {false, false, false};
static bool     relayOn[2] = {false, false};

static bool  probesConfigured = false;
static bool  mpuPresent = false;
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

static bool loadIsOff() {
  return !relayOn[0] && !relayOn[1] && millis() - lastRelayOff >= RELAY_SETTLE_MS;
}

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------

// Zero the current sensor. Only with every relay de-energised and settled: a
// zero taken with the fans running would offset every current reading for the
// life of the boot. Returns whether the zero is plausible.
static bool zeroCurrentSensor() {
  if (!loadIsOff()) return false;
  double sum = 0;
  for (int i = 0; i < 500; i++) { sum += analogReadMilliVolts(PIN_ACS712); delay(1); }
  float zero = (float)(sum / 500.0);
  bool plausible = zero >= ACS_ZERO_MIN_MV && zero <= ACS_ZERO_MAX_MV;
  acsZeroMv = zero;
  acsZeroed = plausible;
  inputRegisters[IREG_ACS_ZERO_MV] = (uint16_t)lroundf(zero);
  setStatus(ST_ACS_ZEROED, plausible);
  Serial.printf("ACS712 zero: %.1f mV (%s)\n", zero,
                plausible ? "plausible" : "IMPLAUSIBLE - current will be invalid");
  return plausible;
}

// Mean current over ~12 ms. The load is DC fans, so the mean is the current;
// it is signed so that a wrong zero shows as a negative current and is caught,
// where an RMS would have hidden the sign and reported it as load.
static float readCurrentAmps(bool &ok) {
  const int samples = 200;
  double sum = 0.0;
  int atRail = 0;
  for (int i = 0; i < samples; i++) {
    float mv = analogReadMilliVolts(PIN_ACS712);
    if (mv <= ACS_RAIL_LOW_MV || mv >= ACS_RAIL_HIGH_MV) atRail++;
    sum += mv;
    delayMicroseconds(50);
  }
  float amps = ((float)(sum / samples) - acsZeroMv) / ACS712_MV_PER_A;
  ok = acsZeroed && atRail < samples / 10 && amps > ACS_NEGATIVE_A;
  return amps;
}

// Vibration as the RMS of acceleration with gravity removed, expressed as an
// equivalent velocity at the fan's running frequency. An approximation, and
// documented as one rather than presented as an ISO 10816 velocity: a proper
// velocity figure needs integration of a calibrated accelerometer signal, and
// this is a bench fan with an MPU-6050.
static float readVibrationMmS(bool &ok) {
  ok = false;
  if (!mpuPresent) return 0.0f;
  const int samples = 64;
  double sum = 0.0;
  sensors_event_t a, g, t;
  for (int i = 0; i < samples; i++) {
    if (!mpu.getEvent(&a, &g, &t)) return 0.0f;
    float magnitude = sqrtf(a.acceleration.x * a.acceleration.x +
                            a.acceleration.y * a.acceleration.y +
                            a.acceleration.z * a.acceleration.z);
    float ac = magnitude - SENSORS_GRAVITY_STANDARD;
    sum += (double)ac * ac;
    delayMicroseconds(500);
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

static void listProbes() {
  DeviceAddress found;
  oneWire.reset_search();
  int count = 0;
  while (oneWire.search(found)) {
    if (OneWire::crc8(found, 7) != found[7]) continue;
    Serial.printf("DS18B20 on bus: {0x%02X,0x%02X,0x%02X,0x%02X,0x%02X,0x%02X,0x%02X,0x%02X}\n",
                  found[0], found[1], found[2], found[3],
                  found[4], found[5], found[6], found[7]);
    count++;
  }
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

  // Zero the current sensor with the load off: the relays are de-energised
  // above and have had time to open. If this is wrong, every current reading is
  // wrong, so a zero outside plausibility leaves current invalid until a good
  // re-zero (coil 2) rather than publishing offset readings.
  delay(RELAY_SETTLE_MS);
  zeroCurrentSensor();

  probesConfigured = addressSet(HUB_PROBE_ADDRESS) &&
                     addressSet(AMBIENT_PROBE_ADDRESS);
  setStatus(ST_PROBES_CONFIG, probesConfigured);
  probes.begin();
  probes.setResolution(12);
  probes.setWaitForConversion(false);          // asynchronous: 750 ms at 12-bit
  listProbes();

  Wire.begin();
  mpuPresent = mpu.begin();
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
#else
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nrig at %s:%u, unit %u\n", WiFi.localIP().toString().c_str(),
                MODBUS_PORT, MODBUS_UNIT_ID);
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
  }

  if (now - lastTempRequest >= DS18B20_INTERVAL_MS) {
    lastTempRequest = now;
    if (tempConversionPending) {
      bool hubOk = false, ambientOk = false;
      inputRegisters[IREG_TEMP_HUB_X10] = asRegister(readProbe(HUB_PROBE_ADDRESS, hubOk));
      inputRegisters[IREG_TEMP_AMB_X10] = asRegister(readProbe(AMBIENT_PROBE_ADDRESS, ambientOk));
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
  // ceiling is invalid, because then the true voltage is unknown. Whether the
  // voltage is acceptable is the monitoring system's judgement (a range rule on
  // RIG_SUPPLY_V), not the sensor's.
  float adcMv = analogReadMilliVolts(PIN_SUPPLY_ADC);
  bool supplyOk = adcMv < SUPPLY_ADC_MAX_MV;
  inputRegisters[IREG_SUPPLY_MV] = supplyOk
      ? (uint16_t)constrain(lroundf(adcMv * SUPPLY_DIVIDER), 0, 65534)
      : INVALID_U16;
  setStatus(ST_SUPPLY_OK, supplyOk);

  discreteInputs[0] = (digitalRead(PIN_RUN_SWITCH) == LOW);
  discreteInputs[1] = relayOn[0];
  discreteInputs[2] = relayOn[1];

  inputRegisters[IREG_SCAN_COUNT]++;
}
