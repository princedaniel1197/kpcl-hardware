# ESP32 bench rig — Modbus register map

The rig is two 12 V fans plus a switchable third, on one supply, instrumented and
published by an ESP32 as a Modbus TCP server (unit id 1).

**It is a bench rig, and the tags say so.** Current is amperes, power is watts,
temperature is degrees Celsius. Nothing here is scaled or renamed to look like a
210 MW unit: 5.5 W of fan is 5.5 W of fan. What the rig demonstrates is real
sensors, a real fieldbus, and quality that survives the whole path — and it
demonstrates that better by being honest about what it is.

## Input registers (function 4, read-only)

| Reg | Quantity | Raw unit | Scale | Engineering unit | Type |
|---|---|---|---|---|---|
| 0 | Load current (ACS712 5 A) | mA | ÷1000 | A | uint16 |
| 1 | Vibration RMS (MPU-6050) | mm/s × 100 | ÷100 | mm/s | uint16 |
| 2 | Motor hub temperature (DS18B20 #1) | °C × 10 | ÷10 | °C | **int16** |
| 3 | Ambient temperature (DS18B20 #2) | °C × 10 | ÷10 | °C | **int16** |
| 4 | Supply voltage | mV | ÷1000 | V | uint16 |
| 5 | **Status word** | bitfield | — | — | uint16 |
| 6 | Firmware scan counter | count | — | — | uint16 |

Registers 2 and 3 are **signed**: a probe below 0 °C is a real reading and an
unsigned register would wrap it to something enormous.

## Discrete inputs (function 2, read-only)

| Bit | Meaning |
|---|---|
| 0 | Run/stop state (rocker switch) |
| 1 | Relay group 1 energised |
| 2 | Relay group 2 energised |

## Coils (function 5/15)

| Coil | Meaning |
|---|---|
| 0 | Relay group 1 command |
| 1 | Relay group 2 command |

Coils exist because the rig has relays and the firmware must be able to stagger
fan starts. **Nothing in the CRPMS acquisition path ever writes them** — the
collector has no write method at all (§303, §315). They are operated from the
rig's own buttons or from a separate bench tool, never from the monitoring
system.

## Status word (input register 5)

| Bit | Set when |
|---|---|
| 0 | DS18B20 #1 (hub) read OK this scan |
| 1 | DS18B20 #2 (ambient) read OK this scan |
| 2 | MPU-6050 responding |
| 3 | ACS712 reading within plausible range |
| 4 | Supply voltage within range |
| 5 | Sensor bus (OneWire) initialised |

A clear bit means **that** measurement is not to be trusted. The rest of the
register set is unaffected.

## Why a status word and not a Modbus exception

The build plan allows either, and asks for the choice to be documented.

A Modbus exception response **aborts the entire read transaction**. The master
asks for registers 0–6 in one request; if an unplugged temperature probe caused
an exception, the master would be denied the current, the vibration, the supply
voltage and the run state as well — all of which are still perfectly good. One
failed sensor would blind the whole rig.

A status bit fails only the point that failed. The master still gets every other
measurement, and gets an explicit statement about the one it cannot trust.

That matches how a real DCS behaves, and it matches what this project claims
everywhere else: quality travels **per point**, alongside the value, rather than
being an all-or-nothing property of the transaction.

## What the temperature register holds when the probe is unplugged

`INT16_MIN` (−32768), which is −3276.8 °C after scaling — not a temperature any
probe can report.

This is belt and braces on purpose. The status bit is the signal; the sentinel
exists so that a master which ignores the status word still cannot mistake the
register for a reading. The DS18B20 returns 85.0 °C on a power-on-reset error
and −127.0 °C on a bus error, and **both are plausible-looking numbers** — 85 °C
is an entirely credible motor hub temperature. Writing either through would be
the substitution this project exists to prevent, so the firmware maps both to
the sentinel and clears the status bit.

The register is never left holding the previous good value. A stale reading that
looks live is worse than no reading.

## Scan behaviour

The firmware scans sensors on its own cycle (default 250 ms) and serves Modbus
from the last completed scan. DS18B20 conversion takes up to 750 ms at 12-bit
resolution, so temperatures are read asynchronously and register 6 counts
completed scans — a master can see the rig is alive even when nothing is moving.

## Fan starts are staggered

Three fans starting together draw about 1.2 A against a 1 A supply. Relay
commands are separated by 500 ms in firmware. This is a property of the rig, not
of the monitoring system.
