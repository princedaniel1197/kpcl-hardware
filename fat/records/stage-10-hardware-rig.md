# Stage 10 — The hardware rig

| | |
|---|---|
| Stage | 10 — The hardware rig |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 10 |
| Acceptance criterion | Unplug the temperature probe. Within one scan the tag goes Bad in the archive, the heat-rate equivalent KPI using it goes Bad, and the dashboard shows why. |
| Date attempted | 2026-09-22 |
| **Result** | **NOT PASSED — the acceptance test requires the physical rig, which has not been built** |

## Why this record says NOT PASSED

The criterion is *unplug the temperature probe*. Everything downstream of the
probe has been built and exercised, but no probe has been unplugged, because
there is no probe: the ESP32, the DS18B20s, the ACS712 and the MPU-6050 are not
on a bench and wired up.

The chain below was verified against a **stand-in Modbus server**
(`firmware/rig_stub.py`) that implements the documented register map. That
proves the bridge, the quality mapping, the acquisition and the KPI. It does not
prove the firmware, the wiring, the pull-up resistor, the sensor behaviour, or
that a real DS18B20 fails the way the datasheet says it does — and unplugging a
simulated probe demonstrates only that the simulation can be told to stop.

Recording this as a pass would be exactly the claim CLAUDE.md forbids.

## What was built

| Item | State |
|---|---|
| `firmware/src/main.cpp` | Complete ESP32 firmware, eModbus TCP server. **Never compiled or flashed** |
| `firmware/platformio.ini` | Build configuration with pinned libraries |
| `firmware/REGISTER_MAP.md` | The contract between firmware and bridge |
| `sim/bridge.py` | Modbus→OPC UA bridge, pymodbus client, published into the Stage 1 server |
| `firmware/rig_stub.py` | Stand-in Modbus server implementing the register map, for testing the bridge |
| Rig tags, asset element, KPI | Configuration only — `config/unit1_tags.json`, `config/asset_model.json`, `config/kpi_definitions.json` |

## What WAS measured, against the stand-in

Bridge → OPC UA, with every sensor healthy:

```
  RIG_CURRENT             0.470 A       Good
  RIG_VIBRATION            2.27 mm/s    Good
  RIG_HUB_TEMP             43.4 degC    Good
  RIG_AMBIENT_TEMP         24.0 degC    Good
  RIG_SUPPLY_V            12.06 V       Good
  RIG_RUNNING              True         Good
```

With the hub probe's status bit cleared — the firmware's stated behaviour when
a DS18B20 read fails:

```
  RIG_CURRENT             0.451 A              Good
  RIG_HUB_TEMP             None       BadDeviceFailure
  RIG_AMBIENT_TEMP         24.1 degC           Good
```

Acquired by the collector with **no code change** and stored in the archive:

```
       name       | count |  avg  | quality
 RIG_AMBIENT_TEMP |    16 | 24.03 | Good
 RIG_CURRENT      |    18 |  0.46 | Good
 RIG_HUB_TEMP     |     1 |       | Bad
 RIG_SUPPLY_V     |    20 | 12.06 | Good
 RIG_VIBRATION    |    20 |  2.34 | Good
```

And the rig's KPI:

```
  MotorThermalRise = (no value) degC   NOT GOOD
    reason: input HubTemperature (RIG_HUB_TEMP) has no value, BadDeviceFailure
```

So the whole path from a cleared status bit to a Bad KPI naming the offending
probe is demonstrated. What is missing is the first link: a real sensor actually
failing.

## Design decisions, and why

**The rig is a bench rig and the tags say so.** Current is amperes, power is
watts, temperature is degrees Celsius. Nothing is scaled or renamed to resemble
a 210 MW unit — 5.5 W of fan is 5.5 W of fan. The build plan says "as a second
unit", and the rig is a second *element*, with its own template, under the same
station. Mapping a fan's 0.46 A onto a tag called `U2_MW` would have made the
demonstration look grander and been a lie.

**A status word, not a Modbus exception.** The plan permits either and asks for
the choice to be documented. A Modbus exception aborts the *entire* read
transaction: one unplugged probe would deny the master the current, the
vibration, the supply voltage and the run state as well, all of which are still
good. A status bit fails only the point that failed. That matches how a DCS
behaves and matches what this project claims everywhere else — quality travels
per point, not as an all-or-nothing property of the transaction.

**The invalid-temperature sentinel.** The DS18B20 returns 85.0 °C after a
power-on reset and −127.0 °C on a bus error, and **85 °C is an entirely credible
motor hub temperature**. Passing either through would be the substitution this
project exists to prevent. The firmware maps both to `INT16_MIN` and clears the
status bit; the bridge refuses the sentinel even if the status bit claims the
read succeeded, and says the two disagree rather than trusting one.

**The KPI is a difference of two measured temperatures.** `MotorThermalRise` =
hub − ambient. No steam table, nothing invented (§486), and it consumes both
probes so unplugging either makes it Bad rather than merely wrong.

## A latent defect this stage exposed

The collector resolved every tag under a **hardcoded `Unit1`** object. Adding
any second unit — the rig, a second simulated unit, a second station — would
have required editing Python to change a string, which is precisely what rule 7
forbids. The build plan's "the collector must require no change" could not have
been true.

Migration `011` adds `tag.source_path`, and the parent object is now
configuration like everything else about a tag. After that one change, adding
the rig was entirely configuration: six tag rows, one element from a template,
one KPI definition.

A second, smaller defect: `collector/seed.py` listed its columns twice, once in
`FIELDS` and once in a SELECT, and adding `source_path` to one left the other
behind — failing with an index error rather than a useful message. The query is
now derived from `FIELDS`.

## To close this record

1. Build the rig per `firmware/REGISTER_MAP.md` — note the **4.7 kΩ pull-up** on
   the OneWire data line, and that both DS18B20s share it by address.
2. Set `WIFI_SSID` and `WIFI_PASSWORD` in `firmware/src/main.cpp`.
3. `cd firmware && pio run -t upload && pio device monitor` — confirm it prints
   the ACS712 zero and the probe count.
4. Note the rig's IP, then:
   ```bash
   make sim SIM_ARGS="--modbus-host <rig-ip>"
   # or: .venv/bin/python -m sim --modbus-host <rig-ip>
   make collector
   ```
5. Confirm the rig tags read plausible values with Good quality.
6. **Unplug the hub DS18B20.** Confirm, within one scan:
   - `RIG_HUB_TEMP` in `sample` has `value IS NULL` and quality 2156593152
   - `MotorThermalRise` is Bad, naming `RIG_HUB_TEMP`
7. Plug it back in; confirm both recover.
8. Replace this record with the measured result.

The firmware has never been compiled. Expect the first `pio run` to need
attention — pin assignments and the ACS712 divider in particular are written
from the specification, not from a working board.

## Tests

`pytest sim/test_bridge.py -q` → **14 passed**: scaling, signed temperatures,
the status-bit-to-BadDeviceFailure mapping, per-point isolation, the sentinel
double-check, the stand-in's protocol handling, and that the bridge issues no
Modbus write and does not live in the collector package.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | |
| Witnessed by | | |
