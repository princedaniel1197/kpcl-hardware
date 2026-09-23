// CRPMS bench rig — configuration that belongs to THIS rig, not to the code.
//
// Committed, because none of it is secret. The WiFi credentials are not here;
// they are in secrets.h, which is not committed (see secrets.h.example).

#pragma once
#include <stdint.h>

// ---------------------------------------------------------------------------
// Relays
// ---------------------------------------------------------------------------
//
// The 1-channel 5 V relay modules on order are ACTIVE-LOW: the coil energises
// when IN is pulled LOW. The first version of this firmware assumed active-high,
// which would have energised both relays at boot, zeroed the current sensor
// with ~0.46 A flowing, and inverted every relay command. If a module turns out
// to be active-high, change this and nothing else.
static const bool RELAY_ACTIVE_LOW = true;

// A 5 V module's IN pin, driven HIGH by a 3.3 V ESP32, may leave ~1.7 V across
// the module's opto-coupler LED -- enough, on some modules, to keep the relay
// energised. If a relay will not release on the bench, set this: the pin then
// switches between driving LOW (energise) and releasing to high impedance
// (de-energise), and the module's own pull-up holds it off. Not verified on the
// hardware on order; it is a setting because only the bench can decide it.
static const bool RELAY_OPEN_DRAIN = false;

// ---------------------------------------------------------------------------
// DS18B20 probes, identified by ROM address
// ---------------------------------------------------------------------------
//
// Both probes share one OneWire bus. They MUST be identified by their 64-bit
// ROM address, not by the order a bus search finds them in: search order
// follows the ROM codes, not the physical positions, so "first found = hub"
// is a coin toss -- and with the hub probe unplugged at boot, the ambient probe
// would be found first and published as the hub. That is a sensor silently
// substituted for another, which is the one thing this rig must never do.
//
// To fill these in: flash with them all zero, open the serial monitor, and the
// firmware prints the address of every probe on the bus every ten seconds.
// Touch one probe and watch which reading rises. Until both are set, both
// temperature points report invalid, and status bit 7 is clear.
static const uint8_t HUB_PROBE_ADDRESS[8]     = {0, 0, 0, 0, 0, 0, 0, 0};
static const uint8_t AMBIENT_PROBE_ADDRESS[8] = {0, 0, 0, 0, 0, 0, 0, 0};

// ---------------------------------------------------------------------------
// Pins (ESP32 38-pin NodeMCU)
// ---------------------------------------------------------------------------
//
// Analogue inputs are on ADC1 deliberately: ADC2 cannot be read while WiFi is
// running.
static const int PIN_ACS712      = 34;   // ADC1_CH6, input only
static const int PIN_SUPPLY_ADC  = 35;   // ADC1_CH7, 12 V through a divider
static const int PIN_ONEWIRE     = 4;    // 4.7 kOhm pull-up to 3.3 V
static const int PIN_RUN_SWITCH  = 27;   // rocker switch to GND, INPUT_PULLUP
static const int PIN_RELAY_1     = 25;   // relay group 1 IN
static const int PIN_RELAY_2     = 26;   // relay group 2 IN

// RS-485 (the `rtu` build only): MAX485 DI <- TX2, RO -> RX2, DE and /RE tied
// together to PIN_RS485_DE.
static const int PIN_RS485_RX    = 16;
static const int PIN_RS485_TX    = 17;
static const int PIN_RS485_DE    = 5;
static const uint32_t RS485_BAUD = 19200;

// ---------------------------------------------------------------------------
// Scaling
// ---------------------------------------------------------------------------

// ACS712ELCTR-05B-T: 185 mV per amp, centred at Vcc/2 (2500 mV on a 5 V
// supply). The zero is measured at boot with the load off, not assumed.
static const float ACS712_MV_PER_A = 185.0f;

// Supply divider ratio, (R_top + R_bottom) / R_bottom. 39 k over 10 k gives
// 12 V -> 2.45 V, inside the range the ESP32 ADC reads linearly at 11 dB; the
// earlier 4:1 put 12 V at 3.0 V, next to the ADC's ceiling, and 13.5 V beyond
// it. Measure the resistors actually fitted and put the real ratio here.
static const float SUPPLY_DIVIDER = (39.0f + 10.0f) / 10.0f;
