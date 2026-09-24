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

// Whether the relays are fitted at all. NOT on 24 Sep 2026: the delivery is
// missing, G25/G26 are unconnected, and the fans run straight from 12 V
// through the ACS712. With this false:
//   - the relay discrete inputs are served as false and status bit 9 is clear,
//     so the bridge publishes RIG_RELAY_1/2 as Bad (BadNotConnected) rather
//     than as a Good "commanded off";
//   - relay coil writes are refused (ILLEGAL DATA ADDRESS);
//   - the ACS712 is zeroed only while the supply reads below the ADC's floor,
//     i.e. while 12 V is disconnected. Operating rule until the relays arrive:
//     USB first, then 12 V. A boot with 12 V already on leaves the current
//     invalid and prints a warning, rather than zeroing the fans into it.
static const bool RELAYS_FITTED = false;

// ---------------------------------------------------------------------------
// Run switch
// ---------------------------------------------------------------------------
//
// NOT fitted on 24 Sep 2026 (G27 unconnected). An unconnected pin reads its
// pull-up, which is "stopped" -- published as Good it would say the rig was
// stopped while the fans ran. With this false, status bit 8 is clear and the
// bridge publishes RIG_RUNNING as Bad (BadNotConnected).
static const bool RUN_SWITCH_FITTED = false;

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
//
// Identified on 24 Sep 2026: with the HUB probe held in the fingers from
// 14:34 UTC, {..,0x4D,..,0x11} rose from 26.6 to 33.2 degC while
// {..,0x99,..,0xD3} stayed at 27.0 degC.
static const uint8_t HUB_PROBE_ADDRESS[8]     = {0x28, 0x4D, 0x31, 0x26, 0x00, 0x00, 0x00, 0x11};
static const uint8_t AMBIENT_PROBE_ADDRESS[8] = {0x28, 0x99, 0xFA, 0x25, 0x00, 0x00, 0x00, 0xD3};

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

// Current direction. +1 if the fans read positive; -1 if the ACS712's IP+/IP-
// are the other way round. A fan load reading clearly negative is reported
// invalid, so a wrong sign shows as a Bad current, and this is where to put it
// right. -1 from 24 Sep 2026: when 12 V was connected at 15:12:25 UTC the
// one-second mean went below -0.10 A within one scan and the current went Bad
// (BadDeviceFailure) in the same second the supply appeared.
static const float ACS712_SIGN = -1.0f;

// Bench correction to the ESP32's calibrated ADC reading on the ACS712 pin.
// Measured 24 Sep 2026 with 12 V off: multimeter ACS712 OUT to GND 2.50 V
// (5 V rail 4.94 V), while analogReadMilliVolts() -- which already applies the
// chip's eFuse calibration -- read 2721-2730 mV. So -225 mV.
//
// What it does and does not do. It makes the zero and register 7 a true
// voltage, so the 2300-2700 mV plausibility window judges the sensor rather
// than the ADC. It cannot change a current: current is a difference from the
// zero, and a constant offset cancels. What it does NOT correct is a gain
// error, which would scale every current reading; one meter reading cannot
// separate gain from offset. 2.5 V is also above the 2450 mV to which
// Espressif characterises this ADC at 11 dB, which is the likely cause. The
// current's scale is verified by comparing it with a meter in series with the
// fans, and is not claimed until that has been done.
static const float ACS712_ADC_OFFSET_MV = -225.0f;

// Supply divider ratio, (R_top + R_bottom) / R_bottom. Measure the resistors
// actually fitted and put the real ratio here.
//
// FITTED on 24 Sep 2026: five 4.7 k, four on top and one below, so 5.0; 12 V
// is 2.4 V on the pin. (Four, as first fitted, put 12 V at 3.0 V against the
// ADC's ceiling.) 2.4 V is still near the top of the range Espressif
// characterises (2450 mV), and on the ACS712 pin this ADC read 225 mV high at
// 2.5 V: until the supply reading has been compared with a meter, it is a
// reading, not a calibrated one. Below 0.75 V (150 mV at the pin) it is
// reported as cannot-measure.
static const float SUPPLY_DIVIDER = (4 * 4.7f + 4.7f) / 4.7f;
