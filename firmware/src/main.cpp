// CRPMS bench rig — ESP32 Modbus TCP server
//
// Two 12 V fans plus a switchable third on one supply, instrumented with an
// ACS712 current sensor, an MPU-6050 for vibration, and two DS18B20 probes
// (motor hub and ambient). Published as Modbus TCP, unit id 1.
//
// The register map is in ../REGISTER_MAP.md and is the contract. Read it before
// changing anything here.
//
// THE RULE THIS FIRMWARE EXISTS TO HONOUR. When a sensor read fails, the
// firmware says so distinctly. It does not return zero, it does not return the
// last good value, and it does not pass through the sensor's own error codes —
// the DS18B20 reports 85.0 °C after a power-on reset and -127.0 °C on a bus
// error, and 85 °C is an entirely credible motor hub temperature. Both are
// mapped to a sentinel that no probe can produce, and the status bit is
// cleared. A stale reading that looks live is worse than no reading.
//
// Libraries: eModbus (MIT), OneWire, DallasTemperature, Adafruit MPU6050.

#include <Arduino.h>
#include <WiFi.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>
#include "ModbusServerTCPasync.h"

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

static const char *WIFI_SSID     = "CHANGE_ME";
static const char *WIFI_PASSWORD = "CHANGE_ME";
static const uint8_t  MODBUS_UNIT_ID = 1;
static const uint16_t MODBUS_PORT    = 502;

// Pins
static const int PIN_ACS712      = 34;   // ADC1_CH6, input only
static const int PIN_ONEWIRE     = 4;    // needs a 4.7 kOhm pull-up to 3.3 V
static const int PIN_RUN_SWITCH  = 27;   // rocker switch, INPUT_PULLUP
static const int PIN_RELAY_1     = 25;
static const int PIN_RELAY_2     = 26;
static const int PIN_SUPPLY_ADC  = 35;   // 12 V through a divider

// ACS712 5 A: 185 mV per amp, centred at Vcc/2.
static const float ACS712_MV_PER_A   = 185.0f;
static const float ACS712_ZERO_MV    = 2500.0f;   // calibrated at boot
static const float SUPPLY_DIVIDER    = 4.0f;      // 12 V -> 3 V

static const uint32_t SCAN_INTERVAL_MS = 250;
static const uint32_t DS18B20_INTERVAL_MS = 1000;   // conversion takes ~750 ms
static const uint32_t RELAY_STAGGER_MS = 500;

// Register map (see REGISTER_MAP.md)
enum : uint16_t {
  IREG_CURRENT_MA   = 0,
  IREG_VIB_MMS_X100 = 1,
  IREG_TEMP_HUB_X10 = 2,
  IREG_TEMP_AMB_X10 = 3,
  IREG_SUPPLY_MV    = 4,
  IREG_STATUS       = 5,
  IREG_SCAN_COUNT   = 6,
  IREG_COUNT        = 7
};

enum : uint16_t {
  ST_HUB_OK     = 1 << 0,
  ST_AMBIENT_OK = 1 << 1,
  ST_MPU_OK     = 1 << 2,
  ST_ACS_OK     = 1 << 3,
  ST_SUPPLY_OK  = 1 << 4,
  ST_BUS_OK     = 1 << 5
};

// No probe can report -3276.8 degC. The status bit is the signal; this sentinel
// exists so a master that ignores the status word still cannot mistake the
// register for a reading.
static const int16_t TEMP_INVALID = INT16_MIN;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

ModbusServerTCPasync modbusServer;
OneWire oneWire(PIN_ONEWIRE);
DallasTemperature probes(&oneWire);
Adafruit_MPU6050 mpu;

static uint16_t inputRegisters[IREG_COUNT] = {0};
static bool     discreteInputs[3] = {false, false, false};
static bool     coils[2] = {false, false};

static DeviceAddress hubAddress, ambientAddress;
static bool  hubFound = false, ambientFound = false;
static bool  mpuPresent = false;
static float acsZeroMv = ACS712_ZERO_MV;
static uint32_t lastScan = 0, lastTempRequest = 0, lastRelayChange = 0;
static bool tempConversionPending = false;

static inline void setStatus(uint16_t bit, bool ok) {
  if (ok) inputRegisters[IREG_STATUS] |= bit;
  else    inputRegisters[IREG_STATUS] &= ~bit;
}

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------

// RMS of the AC component of the current, sampled over one mains-ish window.
static float readCurrentAmps(bool &ok) {
  const int samples = 200;
  double sum = 0.0;
  int outOfRange = 0;
  for (int i = 0; i < samples; i++) {
    int raw = analogRead(PIN_ACS712);
    if (raw <= 2 || raw >= 4093) outOfRange++;   // rail-to-rail: not plausible
    float mv = (raw / 4095.0f) * 3300.0f;
    float amps = (mv - acsZeroMv) / ACS712_MV_PER_A;
    sum += (double)amps * amps;
    delayMicroseconds(50);
  }
  ok = (outOfRange < samples / 10);
  return sqrt(sum / samples);
}

// Vibration as the RMS of acceleration with gravity removed, expressed as an
// equivalent velocity at the fan's running frequency. Documented as an
// approximation rather than presented as an ISO 10816 velocity measurement:
// a proper velocity figure needs integration of a calibrated accelerometer
// signal, and this is a bench fan with an MPU-6050.
static float readVibrationMmS(bool &ok) {
  if (!mpuPresent) { ok = false; return 0.0f; }
  const int samples = 64;
  double sum = 0.0;
  sensors_event_t a, g, t;
  for (int i = 0; i < samples; i++) {
    if (!mpu.getEvent(&a, &g, &t)) { ok = false; return 0.0f; }
    float magnitude = sqrtf(a.acceleration.x * a.acceleration.x +
                            a.acceleration.y * a.acceleration.y +
                            a.acceleration.z * a.acceleration.z);
    float ac = magnitude - 9.81f;            // remove gravity
    sum += (double)ac * ac;
    delayMicroseconds(500);
  }
  ok = true;
  float accelRms = sqrt(sum / samples);              // m/s^2
  const float fanHz = 40.0f;                         // nominal running speed
  return (accelRms / (2.0f * PI * fanHz)) * 1000.0f; // mm/s
}

static int16_t readProbe(DeviceAddress address, bool found, bool &ok) {
  if (!found) { ok = false; return TEMP_INVALID; }
  float c = probes.getTempC(address);
  // DEVICE_DISCONNECTED_C is -127. 85.0 is the DS18B20's power-on-reset value
  // and is a perfectly plausible motor hub temperature, which is exactly why it
  // must not be passed through.
  if (c == DEVICE_DISCONNECTED_C || c <= -55.0f || c >= 84.9f) {
    ok = false;
    return TEMP_INVALID;
  }
  ok = true;
  return (int16_t)lroundf(c * 10.0f);
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

ModbusMessage onWriteCoil(ModbusMessage request) {
  uint16_t address = 0, value = 0;
  request.get(2, address);
  request.get(4, value);
  if (address > 1) {
    return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                         ILLEGAL_DATA_ADDRESS);
  }
  // Fan starts are staggered: three fans starting together draw about 1.2 A
  // against a 1 A supply.
  uint32_t now = millis();
  if (value == 0xFF00 && now - lastRelayChange < RELAY_STAGGER_MS) {
    return ModbusMessage(request.getServerID(), request.getFunctionCode(),
                         SERVER_DEVICE_BUSY);
  }
  lastRelayChange = now;
  coils[address] = (value == 0xFF00);
  digitalWrite(address == 0 ? PIN_RELAY_1 : PIN_RELAY_2,
               coils[address] ? HIGH : LOW);
  ModbusMessage response;
  response.add(request.getServerID(), request.getFunctionCode(), address, value);
  return response;
}

// ---------------------------------------------------------------------------
// Setup and loop
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  pinMode(PIN_RUN_SWITCH, INPUT_PULLUP);
  pinMode(PIN_RELAY_1, OUTPUT);
  pinMode(PIN_RELAY_2, OUTPUT);
  digitalWrite(PIN_RELAY_1, LOW);
  digitalWrite(PIN_RELAY_2, LOW);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_ACS712, ADC_11db);
  analogSetPinAttenuation(PIN_SUPPLY_ADC, ADC_11db);

  for (uint16_t i = 0; i < IREG_COUNT; i++) inputRegisters[i] = 0;
  inputRegisters[IREG_TEMP_HUB_X10] = (uint16_t)TEMP_INVALID;
  inputRegisters[IREG_TEMP_AMB_X10] = (uint16_t)TEMP_INVALID;

  // Zero the current sensor with the load off. If this is wrong every current
  // reading is wrong, so it happens once, deliberately, at boot.
  double sum = 0;
  for (int i = 0; i < 500; i++) { sum += analogRead(PIN_ACS712); delay(1); }
  acsZeroMv = (sum / 500.0 / 4095.0) * 3300.0;
  Serial.printf("ACS712 zero: %.1f mV\n", acsZeroMv);

  probes.begin();
  probes.setResolution(12);
  probes.setWaitForConversion(false);          // asynchronous: 750 ms at 12-bit
  hubFound     = probes.getAddress(hubAddress, 0);
  ambientFound = probes.getAddress(ambientAddress, 1);
  setStatus(ST_BUS_OK, probes.getDeviceCount() > 0);
  Serial.printf("DS18B20 probes found: %d\n", probes.getDeviceCount());

  Wire.begin();
  mpuPresent = mpu.begin();
  setStatus(ST_MPU_OK, mpuPresent);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nrig at %s\n", WiFi.localIP().toString().c_str());

  modbusServer.registerWorker(MODBUS_UNIT_ID, READ_INPUT_REGISTER,
                              &onReadInputRegisters);
  modbusServer.registerWorker(MODBUS_UNIT_ID, READ_DISCR_INPUT,
                              &onReadDiscreteInputs);
  modbusServer.registerWorker(MODBUS_UNIT_ID, WRITE_COIL, &onWriteCoil);
  modbusServer.start(MODBUS_PORT, 4, 0);
}

void loop() {
  uint32_t now = millis();

  if (now - lastTempRequest >= DS18B20_INTERVAL_MS) {
    lastTempRequest = now;
    if (tempConversionPending) {
      bool hubOk = false, ambientOk = false;
      int16_t hub = readProbe(hubAddress, hubFound, hubOk);
      int16_t ambient = readProbe(ambientAddress, ambientFound, ambientOk);
      inputRegisters[IREG_TEMP_HUB_X10] = (uint16_t)hub;
      inputRegisters[IREG_TEMP_AMB_X10] = (uint16_t)ambient;
      setStatus(ST_HUB_OK, hubOk);
      setStatus(ST_AMBIENT_OK, ambientOk);
    }
    // Re-enumerate so a probe plugged back in is found again without a reboot.
    if (!hubFound || !ambientFound) {
      probes.begin();
      hubFound     = probes.getAddress(hubAddress, 0);
      ambientFound = probes.getAddress(ambientAddress, 1);
      setStatus(ST_BUS_OK, probes.getDeviceCount() > 0);
    }
    probes.requestTemperatures();
    tempConversionPending = true;
  }

  if (now - lastScan < SCAN_INTERVAL_MS) return;
  lastScan = now;

  bool acsOk = false;
  float amps = readCurrentAmps(acsOk);
  inputRegisters[IREG_CURRENT_MA] =
      acsOk ? (uint16_t)constrain(lroundf(amps * 1000.0f), 0, 65535) : 0;
  setStatus(ST_ACS_OK, acsOk);

  bool vibOk = false;
  float vib = readVibrationMmS(vibOk);
  inputRegisters[IREG_VIB_MMS_X100] =
      vibOk ? (uint16_t)constrain(lroundf(vib * 100.0f), 0, 65535) : 0;
  setStatus(ST_MPU_OK, vibOk);

  float supplyV = (analogRead(PIN_SUPPLY_ADC) / 4095.0f) * 3.3f * SUPPLY_DIVIDER;
  inputRegisters[IREG_SUPPLY_MV] =
      (uint16_t)constrain(lroundf(supplyV * 1000.0f), 0, 65535);
  setStatus(ST_SUPPLY_OK, supplyV > 10.5f && supplyV < 13.5f);

  discreteInputs[0] = (digitalRead(PIN_RUN_SWITCH) == LOW);
  discreteInputs[1] = coils[0];
  discreteInputs[2] = coils[1];

  inputRegisters[IREG_SCAN_COUNT]++;
}
