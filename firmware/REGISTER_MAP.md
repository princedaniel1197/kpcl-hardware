# ESP32 bench rig — Modbus register map, version 3

The rig is two 12 V fans plus a switchable third, on one supply, instrumented and
published by an ESP32 as Modbus unit id 1 — over **TCP** (WiFi, port 502) or over
**RTU** (RS-485 through a MAX485, 19200 baud 8N1). Both builds serve exactly this
map.

**It is a bench rig, and the tags say so.** Current is amperes, temperature is
degrees Celsius. Nothing here is scaled or renamed to look like a 210 MW unit:
5.5 W of fan is 5.5 W of fan. What the rig demonstrates is real sensors, a real
fieldbus, and quality that survives the whole path — and it demonstrates that
better by being honest about what it is.

Version 2 came out of the code review of 23 September 2026 and the first compile.
Version 3 came out of the first power-up on the bench, 24 September 2026. What
changed in each is listed at the end.

## Input registers (function 4, read-only)

| Reg | Quantity | Raw unit | Scale | Engineering unit | Type | Invalid sentinel |
|---|---|---|---|---|---|---|
| 0 | Load current, mean (ACS712 5 A) | mA | ÷1000 | A | **int16** | −32768 |
| 1 | Vibration RMS (MPU-6050 or MPU-6500) | mm/s × 100 | ÷100 | mm/s | uint16 | 65535 |
| 2 | Motor hub temperature (DS18B20) | °C × 10 | ÷10 | °C | **int16** | −32768 |
| 3 | Ambient temperature (DS18B20) | °C × 10 | ÷10 | °C | **int16** | −32768 |
| 4 | Supply voltage | mV | ÷1000 | V | uint16 | 65535 |
| 5 | **Status word** | bitfield | — | — | uint16 | — |
| 6 | Firmware scan counter | count | — | — | uint16 | — |
| 7 | ACS712 zero, as measured, after the bench ADC correction | mV | — | mV | uint16 | 0 = never zeroed |

Registers 0, 2 and 3 are **signed**. A probe below 0 °C is a real reading, and
so is a current slightly below zero — which on a load of fans means the zero is
wrong, and a signed register is how that shows instead of being hidden.

## Status word (input register 5)

| Bit | Set when |
|---|---|
| 0 | Hub probe read OK this conversion |
| 1 | Ambient probe read OK this conversion |
| 2 | Accelerometer (MPU-6050 or MPU-6500, by WHO_AM_I) responding and read OK this scan |
| 3 | Current reading valid: sensor zeroed, output not at a rail, mean not implausibly negative |
| 4 | Supply within the ADC's measurable range — above its floor (150 mV at the pin) and below saturation |
| 5 | At least one device answers on the OneWire bus |
| 6 | ACS712 zeroed with the load off, and the zero is plausible (2300–2700 mV) |
| 7 | Both probe ROM addresses are configured (`src/rig_config.h`) |
| 8 | Run switch fitted (`RUN_SWITCH_FITTED`) |
| 9 | Relays fitted (`RELAYS_FITTED`) |

A clear bit means **that** measurement is not to be trusted, and its register
holds its sentinel. The rest of the register set is unaffected.

**Bits 8 and 9 are about the hardware, not a reading.** An input with nothing
wired to it still reads: the run-switch pin floats to its pull-up, which is
"stopped", and a relay that is not there reads "commanded off". Published as
Good, either would be a statement about the rig that nobody measured. With the
bit clear the bridge publishes the input as **BadNotConnected**, with no value,
and relay coil writes are refused (ILLEGAL DATA ADDRESS).

**Bit 4 is about the ADC, not the supply.** A supply reading of 9.8 V during a
brown-out is a valid measurement, and exactly the one worth having; version 1
cleared the bit outside 10.5–13.5 V and so turned it into BadDeviceFailure with
no value. Whether a voltage is acceptable is the monitoring system's judgement —
a range rule on `RIG_SUPPLY_V` — not the sensor's. The bit is also clear at
the other end: with 12 V disconnected this board's ADC reads about 142 mV on the
divider, which version 2 published as a Good 0.57 V.

## Discrete inputs (function 2, read-only)

| Bit | Meaning |
|---|---|
| 0 | Run/stop state (rocker switch closed to GND) |
| 1 | Relay group 1 **commanded** on |
| 2 | Relay group 2 **commanded** on |

Bits 1 and 2 report the command, not the contacts: the relay modules have no
feedback, and the names in the OPC UA address space say "commanded".

## Coils (function 5, write single coil)

| Coil | Meaning |
|---|---|
| 0 | Relay group 1 command |
| 1 | Relay group 2 command |
| 2 | Re-zero the current sensor (write ON) |

A value other than 0xFF00 or 0x0000 is refused with ILLEGAL DATA VALUE. Turning a
relay on within 500 ms of another turning on is refused with SERVER DEVICE BUSY
(see *Fan starts*). A re-zero is refused with SERVER DEVICE BUSY while either
relay is commanded on or has been off for less than 300 ms — a zero taken with
the fans running would offset every current reading.

The coils are the rig's own control surface, operated from a bench tool. **Nothing
in the CRPMS acquisition path ever writes them** — the bridge only reads (a test
asserts it issues no Modbus write), and the collector has no write method at all
(§303, §315).

## Relays are active-low

The 5 V relay modules energise their coil when IN is pulled **LOW**. Version 1
assumed active-high: at boot it drove both pins LOW, energising both relays and
starting every fan before WiFi or any command; it then zeroed the current sensor
with ~0.46 A flowing; and every ON command switched a relay OFF. Now:

- polarity is one setting, `RELAY_ACTIVE_LOW` in `src/rig_config.h`;
- at boot the de-energised level is written **before** the pins become outputs, and again after (to be confirmed on the bench, below);
- the current sensor is zeroed only after both relays are de-energised and
  settled, and can be re-zeroed on the bench through coil 2.

This assumes every fan is behind a relay. A fan wired straight to the supply
would be running during the zero, and the zero would be wrong.

Two things only the bench can settle, and the firmware makes each a setting
rather than a guess:

- **No relay should click at power-on or reset.** The level is written before
  and after each pin becomes an output, and the modules' pull-ups hold them off
  until then; confirm it.
- **A relay must release when commanded off.** A 3.3 V HIGH on a 5 V module's IN
  can leave enough across its opto-coupler to hold some modules on. If one will
  not release, set `RELAY_OPEN_DRAIN` in `rig_config.h`: off then means the pin
  lets go rather than drives HIGH.

## The DS18B20 probes are identified by ROM address

Both probes share one OneWire wire, and each is read by its 64-bit ROM address,
set in `src/rig_config.h`. Version 1 took "first found on the bus" as the hub and
"second" as ambient. Bus search order follows the ROM codes, not where the probes
are, so which probe was called the hub was a coin toss — and with the hub probe
unplugged at boot, the ambient probe was found first and published as the hub
temperature. That is one sensor silently standing in for another.

Until both addresses are configured, both temperatures report invalid, bit 7 is
clear, the bridge publishes **BadConfigurationError**, and the firmware prints
every ROM address on the bus to the serial monitor every ten seconds so they can
be filled in.

## Why a status word and not a Modbus exception

The build plan allows either, and asks for the choice to be documented.

A Modbus exception response **aborts the entire read transaction**. The master
asks for registers 0–7 in one request; if an unplugged temperature probe caused
an exception, the master would be denied the current, the vibration, the supply
voltage and the run state as well — all of which are still perfectly good. One
failed sensor would blind the whole rig.

A status bit fails only the point that failed. The master still gets every other
measurement, and gets an explicit statement about the one it cannot trust. That
matches what this project claims everywhere else: quality travels **per point**,
alongside the value, rather than being an all-or-nothing property of the
transaction.

## What a register holds when its sensor has failed

Its sentinel — a value no sensor on this rig can produce — and never zero, never
the previous good value. Zero amps and zero vibration are exactly what a stopped
fan reads, so version 1's zeros on a failed current or vibration read were
indistinguishable from a stopped rig to any master that ignored the status word.

The DS18B20 returns 85.0 °C from its power-on-reset register and −127 °C when no
device answers. **85 °C is a credible motor hub temperature**, so the firmware
treats exactly 85.0 as invalid. A genuine reading of exactly 85.0000 °C is
sacrificed to that, and reports invalid; everything else from −55 to 125 °C is
passed through. (Version 1 rejected everything from 84.9 °C up, which would have
turned an overheating motor — the anomaly worth seeing — into a failed sensor.)

## How the bridge maps it

| Condition | OPC UA StatusCode on the tag | Value |
|---|---|---|
| status bit set, register not a sentinel | Good | scaled reading |
| probe addresses not configured (bit 7 clear) | BadConfigurationError | null |
| supply below the ADC's floor or at its ceiling (bit 4 clear) | BadOutOfRange | null |
| current sensor not zeroed (bit 6 clear) | BadDeviceFailure, the reason naming the zero | null |
| run switch or relays not fitted (bit 8 or 9 clear) | BadNotConnected on that discrete input | null |
| any other clear status bit | BadDeviceFailure | null |
| status bit set but register holds the sentinel | BadDeviceFailure, logged as a firmware/bridge disagreement | null |
| rig unreachable, or a Modbus exception | BadNoCommunication on every point | null |

## Scan behaviour

The firmware scans sensors on its own cycle (250 ms) and serves Modbus from the
last completed scan. DS18B20 conversion takes up to 750 ms at 12-bit resolution,
so temperatures are converted asynchronously once a second; register 6 counts
completed scans, so a master can see the rig is alive even when nothing is moving.

The current is the **mean** of 200 samples over about 12 ms. The load is DC fans,
so the mean is the current. Version 1 took an RMS, which throws away the sign —
a wrong zero then read as load instead of as an impossible negative current.

Analogue inputs use `analogReadMilliVolts()`, which applies the ESP32's factory
ADC calibration, not `raw / 4095 × 3.3 V`: at 11 dB attenuation the ADC's range
ends near 3.1 V, not 3.3 V, and is non-linear towards the top.

## Wiring

| Signal | ESP32 pin | Notes |
|---|---|---|
| ACS712 OUT | GPIO 34 (ADC1) | ACS712 on 5 V; output 2.5 V ± 0.185 V/A |
| Supply divider | GPIO 35 (ADC1) | Fitted: four 4.7 kΩ over one, 5.0 — 12 V → 2.4 V. Put the fitted ratio in `SUPPLY_DIVIDER` |
| DS18B20 data | GPIO 4 | **4.7 kΩ pull-up to 3.3 V**; both probes on this one wire |
| Run switch | GPIO 27 | to GND; internal pull-up |
| Relay 1 IN | GPIO 25 | active-low module |
| Relay 2 IN | GPIO 26 | active-low module |
| MPU-6050 / MPU-6500 | GPIO 21 SDA / 22 SCL | I²C, address 0x68 |
| MAX485 DI / RO | GPIO 17 (TX2) / 16 (RX2) | `rtu` build only |
| MAX485 DE + /RE | GPIO 5 | tied together; driven by the Modbus server |

All three fans sit downstream of the ACS712, so it measures total load. ADC2 is
not used: it cannot be read while WiFi is on.

## Fan starts are staggered

Three fans starting together draw about 1.2 A against a 1 A supply. A relay ON
command within 500 ms of the previous one is refused (SERVER DEVICE BUSY), so the
caller retries rather than the firmware queueing starts it may no longer want.

## Changes from version 1

| | Version 1 | Version 2 |
|---|---|---|
| Relay polarity | active-high assumed; relays energised at boot | active-low, one setting; de-energised before the pins are outputs |
| Current zero | at boot, with the fans running | after the relays are open and settled; plausibility-checked; re-zero coil |
| Current | uint16, RMS | int16, mean — a wrong zero shows as negative |
| Failed current / vibration / supply | 0 | sentinel |
| Probe identity | bus search order | configured ROM address |
| 85 °C and above | rejected | only exactly 85.0 rejected; −55 to 125 passed |
| Supply status bit | 10.5–13.5 V | ADC not saturated |
| Register 7, bits 6–7 | — | ACS712 zero; zeroed; probes configured |
| Coil values other than ON/OFF | accepted as OFF | refused |
| Transport | TCP only | TCP or RTU (RS-485) |
| Compiled | never | both builds, 23 Sep 2026, PlatformIO espressif32 7.1.3 — not yet flashed |

## Changes from version 2 (bench bring-up, 24 September 2026)

| | Version 2 | Version 3 |
|---|---|---|
| Accelerometer | Adafruit MPU6050 driver; refuses anything but WHO_AM_I 0x68 | registers read directly; accepts MPU-6050 (0x68) and MPU-6500 (0x70), nothing else. The module on the bench is an MPU-6500 |
| Vibration | RMS of \|a\| − 9.80665 m/s² — a sensor scale error read as vibration (1.02 mm/s at rest) | RMS about the window's own mean (0.40–0.46 mm/s at rest: the noise floor) |
| Supply status bit | clear only at the ADC's ceiling | clear at the floor too: 12 V disconnected read as 0.57 V |
| Status bits 8, 9 | — | run switch fitted; relays fitted. Absent hardware publishes BadNotConnected |
| Relay coils | always accepted | refused when the relays are not fitted |
| Current zero | with the relays open | also, with no relays, only while the supply reads below the ADC floor (12 V off); otherwise refused and a warning printed |
| ACS712 reading | `analogReadMilliVolts()` | the same plus a bench offset measured against a multimeter (−225 mV); `ACS712_SIGN` for a reversed sensor |
| Serial | probe addresses | addresses with each probe's reading and role; I²C scan; ADC calibration source; a status line every 10 s |
