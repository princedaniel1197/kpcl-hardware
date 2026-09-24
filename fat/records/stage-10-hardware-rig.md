# Stage 10 — The hardware rig

| | |
|---|---|
| Stage | 10 — The hardware rig |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 10 |
| Acceptance criterion | Unplug the temperature probe. Within one scan the tag goes Bad in the archive, the heat-rate equivalent KPI using it goes Bad, and the dashboard shows why. |
| Date attempted | 2026-09-22 (not possible: no rig); run on the bench 2026-09-24 |
| **Result** | **PASSED on 24 September 2026, run 2, 15:31 UTC**, after run 1 found a defect on the replug. Limits: the current's scale is not verified and its zero drifts (see the 24 September addendum) |

## Why this record said NOT PASSED on 22 September

*Superseded by the 24 September addendum below, which records the test run on the
real rig. Kept because it is why nothing was claimed before then.*

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

## Addendum — 23 September 2026: the firmware compiles; four defects fixed

Still **NOT PASSED** — nothing has been flashed and no probe unplugged. What
changed (details in `fat/records/review-2026-09-23.md`, A1):

- **It compiles**, both builds, `-Wall -Wextra`, versions pinned: TCP 61.6 %
  flash, RTU 26.0 %. It never could have before: the eModbus line in
  `platformio.ini` named a package the registry does not have.
- **Relays are active-low.** The version above would have energised both relays
  at boot and zeroed the current sensor with the fans running.
- **Probes are identified by ROM address.** The version above took the first
  probe on the bus as the hub; with the hub unplugged at boot, which is this
  stage's test, the ambient probe would have been published as the hub.
- Register map version 2: signed mean current, a sentinel on every channel, the
  supply bit meaning "measurable", 85.0 °C alone rejected, a zero register, a
  re-zero coil. The bridge maps unconfigured probes to `BadConfigurationError`
  and a saturated supply ADC to `BadOutOfRange`.
- An RTU build over RS-485, and a bridge that reads either.

The steps in "To close this record" change: copy `src/secrets.h.example` to
`src/secrets.h` instead of editing `main.cpp`; `pio run -e tcp -t upload`; then
**read the two probe ROM addresses from the serial monitor into
`src/rig_config.h`** and flash again — until then both temperatures are
invalid by design. Confirm the serial monitor prints a *plausible* ACS712 zero,
that no relay clicks at power-on or reset, and that each relay releases when
commanded off (if one does not, set `RELAY_OPEN_DRAIN`; see the register map).

`sim/test_bridge.py`: 24 tests, including the path end to end through the
stand-in over real Modbus TCP into a real OPC UA server.

## Addendum — 24 September 2026: flashed and powered on the bench

Still **NOT PASSED**: the test — unplug the hub probe and see Bad, with no
value, downstream — has not been run through the bridge, and the WiFi build has
not been flashed. But the firmware now runs on the real board, against its real
sensors. The RS-485 build was flashed over USB (`/dev/cu.usbserial-0001`, no
BOOT button needed) because the WiFi build waits at start-up for a network.

**The bench as wired** differs from the plan: no relays (the delivery is
missing; the fans run straight from 12 V through the ACS712), no rocker switch,
the supply divider built from five 4.7 kΩ resistors (5.0), and 12 V not yet
connected. The firmware now says so rather than assuming the plan
(`RELAYS_FITTED`, `RUN_SWITCH_FITTED` in `rig_config.h`).

What the board reported, 12 V off, from the serial monitor:

| | Reading |
|---|---|
| DS18B20 | **2 found**. With the HUB probe held from 14:34 UTC, `{0x28,0x4D,0x31,0x26,0,0,0,0x11}` rose 26.6 → 33.2 °C while `{0x28,0x99,0xFA,0x25,0,0,0,0xD3}` stayed at 27.0 °C: HUB and AMBIENT respectively, now in `rig_config.h` |
| Accelerometer | answers at 0x68 with **WHO_AM_I 0x70: an MPU-6500**, not the MPU-6050 it was sold as. The Adafruit driver refused it; the firmware now reads both parts' (identical) accelerometer registers directly |
| Vibration, at rest | 1.02 mm/s as first computed — a scale error, from subtracting standard gravity; **0.40–0.46 mm/s** once taken about the window's mean, which is the sensor's noise floor |
| ACS712 zero | the ESP32 read **2,721–2,730 mV**, outside the 2,300–2,700 window, and the firmware refused it. A multimeter read **2.50 V** on OUT (5 V rail 4.94 V): the ADC reads 225 mV high there, although `analogReadMilliVolts()` already applies the chip's calibration (eFuse Vref). 2.5 V is above the 2,450 mV Espressif characterises at 11 dB. A −225 mV bench correction is now configured; the zero reads **2,493 mV** and current **0.000 A ± 0.004** with no load |
| Supply | read 142 mV at the pin, published by the old firmware as a Good **0.57 V** with nothing connected. Below the ADC's floor it is now "cannot measure" (BadOutOfRange) |
| ADC calibration | eFuse Vref |

The correction cancels in the current (a difference from the zero); what it
cannot touch is a gain error, which would scale every current. **The current's
scale is not claimed** until it has been compared with a meter in series with
the fans, and the supply reading until it has been compared with a meter on
12 V.

Without relays, the only protection for the zero is order: **USB first, then
12 V**. The firmware enforces it — it zeroes only while the supply reads below
the ADC floor, and otherwise leaves the current invalid and prints a warning —
because a zero taken with the fans running would publish every current about
0.46 A wrong, as Good, and pass every plausibility check.

Register map version 3 records all of it. `sim/test_bridge.py`: 30 tests.

**WiFi build, later on 24 September.** Flashed and on the network (a DHCP address
on the bench LAN, 192.168.1.89 that day). Two failures first, both found by the
firmware saying why instead of printing dots: the building's network was 5 GHz
only, which the ESP32 cannot see (reason 201, 16 other 2.4 GHz networks
visible), and then the 2.4 GHz network's name differed from `secrets.h` in the
case of one letter. From this Mac over Modbus TCP, 60 polls a second apart,
12 V still off:

| | |
|---|---|
| Polls | 60 of 60 answered, no errors |
| Round trip | median 32 ms, P95 310 ms, max 539 ms (WiFi) |
| Scan counter | 124 → 380 in the minute, never went back: no reset, ~4 scans/s as designed |
| Status | 0x0EF: probes, accelerometer, current, bus, zero and addresses OK; supply cannot-measure (12 V off); switch and relays not fitted |
| Hub / ambient | 29.5 / 26.6 °C |
| Vibration, at rest | 0.36–0.53 mm/s |
| Current, **no load** | **−0.069 to +0.005 A**, mean −0.038 A |

**The current is not yet good enough to claim.** Before WiFi connected the
no-load current read 0.000 ± 0.004 A; with the radio running it wanders by
70 mA — 15 % of the fans' 0.46 A — and averages 38 mA low. The zero is taken
before WiFi starts, so it does not include whatever the radio does to the 5 V
rail or the ADC. Not yet fixed; see the next steps.

**Current: zero after WiFi, longer averaging.** The zero is now taken once WiFi
is up (still only while the supply reads no 12 V), WiFi power saving is off
(the radio's wake bursts were moving the reading, and delayed ARP replies), and
the published current is the mean of 400 samples a scan over the last four
scans (one second). Fans off, polled once a second over Modbus:

| | No-load current | Spread |
|---|---|---|
| Before: 200 samples / 12 ms, zero before WiFi, modem sleep | −69 to +5 mA, mean −38 | SD 26 mA |
| After, first minute from boot | 0 to +13 mA, mean +4.9 | SD 3.0 mA |
| After, minutes 2–3 | +3 to +13 mA, mean +8.5 | SD 2.2 mA |
| After, per minute from the archive, 15:09–15:12 | means +12, +21, +25, +23 mA; max +31 mA | — |

**The noise is inside ±10 mA; the zero is not.** Random scatter fell from an
SD of 26 mA to about 2–3 mA, but the zero drifted upward by about 25 mA
(≈ 5 mV at the ADC) over the six minutes after it was taken. Whether it is the
ESP32's ADC warming (the radio now runs continuously, and 2.5 V is above the
range the ADC is characterised for) or the ACS712 has not been separated: that
needs a meter on OUT while the reading drifts. The ±10 mA target is **not met**.

**The rig through the bridge.** The simulator was restarted at 15:09 UTC with
`--modbus-host 192.168.1.89`: the bridge reads the real rig, the collector
archives it, and the API and the dashboard's Health tab show it —
RIG_CURRENT, RIG_VIBRATION, RIG_HUB_TEMP and RIG_AMBIENT_TEMP Good,
RIG_SUPPLY_V BadOutOfRange with no value while 12 V was off, and RIG_RUNNING
BadNotConnected with no value.

**12 V, 15:12:25 UTC.** The supply read 11.79 V, then 12.09–12.15 V (Good; not
yet compared with a meter). In the same second the current went Bad: the
one-second mean fell below −0.10 A within a scan of the fans starting, so the
ACS712 is reversed, which is what the negative check is for. `ACS712_SIGN` is
now −1. Vibration stayed at 0.4 mm/s with the fans on.

**12 V with the sign corrected, 15:19 UTC.** Zero taken with 12 V off
(2,439.6 mV; it was 2,515 mV at the previous boot). Once 12 V was connected:
current Good and positive, 1.16 A while the fans spun up, then 0.87–0.93 A
steady (mean 0.878 A over 60 s); supply 12.04–12.12 V (the user's meter: ~12 V);
status 0x0FF. **0.88 A is about twice the 0.46 A the fans' ratings add up to**,
and the ADC's error around 2.5 V can inflate a difference from the zero. The
current's scale is not claimed until it has been compared with a meter in
series. (At 15:23 the reading fell to about 0.30 A, with a spike to 1.26 A,
while the test below was being set up; not yet explained.) Accepted for now as
documented limits, by the user: the zero drift, and the unverified scale.

### Acceptance test, run 1 — 24 September, 15:24 UTC

The hub probe unplugged at the bench, then plugged back in.

| Time (UTC) | RIG_HUB_TEMP in `sample` | MotorThermalRise |
|---|---|---|
| 15:23:33.9 | 29.3 °C, Good (last sample before; archived on change) | 2.70 °C Good (15:23:57) |
| 15:24:02.8 | **NULL, 2156593152 (BadDeviceFailure)**, the same poll the bridge logged it | **Bad, NULL** from 15:24:07: "input HubTemperature (RIG_HUB_TEMP) has no value, BadDeviceFailure", four calculations |
| 15:24:33.6 | NULL, BadDeviceFailure | Bad |
| 15:24:38.7 | **99.3 °C, Good** — wrong | — |
| 15:24:39.8 | 29.3 °C, Good | 2.70 °C Good (15:24:48) |

During the fault RIG_AMBIENT_TEMP, RIG_CURRENT, RIG_VIBRATION and RIG_SUPPLY_V
stayed Good; the dashboard's Health tab showed RIG_HUB_TEMP bad with its
StatusCode. No Bad hub sample carries a value.

**The unplug passed; the replug did not.** The first reading after the probe
came back was 99.3 °C with a valid CRC — a probe powering up on the bus
mid-sequence — and it was published and archived as Good. That is a Good
value that was never a measurement. The KPI happened not to read it (it
calculates every 10 s); a rule or alert might have. The firmware now trusts a
returning probe (and every probe at boot) only once two successive conversions
agree within 1.0 °C; until then the point is invalid. **The 99.3 °C sample is
left in the archive**, where it is the evidence for this defect; it is not
deleted or edited.

Stage 10 is **not passed** until the test is re-run with that fix and the
replug publishes nothing unconfirmed.

**The current moved again.** From 15:23:38 the reading fell from 0.88 A to a
steady 0.23 A (a 1.26 A spike and a supply dip to 11.65 V in the same second),
yet the user found **all three fans spinning**. Re-zeroed at the next boot
(2,431.9 mV; just before it, with 12 V off, the old zero read +39 mA) and with
12 V on again: **0.194–0.213 A**, mean 0.203 A, 47 of 47 Good. Same fans, 0.88 A
in one boot and 0.20 A in another. The current reading is **not a
measurement of the fans' current** until the in-series meter check and the
drift investigation are done; the rig's other points do not depend on it.

### Acceptance test, run 2 — 24 September, 15:31 UTC, with the confirmation fix

| Time (UTC) | RIG_HUB_TEMP in `sample` | MotorThermalRise |
|---|---|---|
| 15:31:10.1 | 29.1 °C, Good (last before) | 2.5 °C Good (15:31:34) |
| 15:31:39.9 | **NULL, BadDeviceFailure (0x808B0000)** — the bridge logged it at 15:31:39.949, the same poll | **Bad, NULL** from 15:31:45, "input HubTemperature (RIG_HUB_TEMP) has no value, BadDeviceFailure"; seven calculations |
| 15:32:13.9, 15:32:43.7 | NULL, BadDeviceFailure | Bad |
| 15:32:54.0 | **29.1 °C, Good — the first Good value after the replug**, once two conversions agreed | 2.5 °C Good from 15:32:56 |
| 15:33:03–15:33:18 | 28.9, 29.1, 29.2, 29.1 °C | Good |

No Bad hub sample in the archive carries a value. RIG_CURRENT (70 samples),
RIG_SUPPLY_V (72) and RIG_VIBRATION (68) stayed Good throughout.
**RIG_AMBIENT_TEMP was Bad once**, NULL, at 15:32:48.8 — the moment the hub
probe went back onto the bus they share — and Good again at 15:32:50.9 after
two agreeing conversions. Plugging a device into a shared OneWire line can
corrupt the other device's read; the firmware reported that read as failed,
with no value, rather than as a temperature. The unplug itself (69 s) did not
touch the ambient point.

The archive is Bad in the bridge poll that first saw the firmware clear the
bit; the firmware converts once a second. The physical moment of unplugging
was not timed, so "within one scan" is measured from the firmware's report,
not from the hand on the connector.

**The dashboard shows why** — observed at 15:35:15 UTC during a third,
short unplug for the purpose (RIG_HUB_TEMP NULL, BadDeviceFailure from
15:34:59.5): the MotorThermalRise card on the Pipeline view turned red, value
"- - -", with the text "input HubTemperature (RIG_HUB_TEMP) has no value,
BadDeviceFailure"; the Health tab listed RIG_HUB_TEMP bad with its StatusCode
(seen in run 1).

### Result

**PASSED.** On the real rig, through real Modbus TCP over WiFi, the bridge, a
real OPC UA server, the collector and the archive: unplugging the hub probe put
RIG_HUB_TEMP Bad with no value in the poll that first saw it, MotorThermalRise
Bad naming the probe from its next calculation, and the dashboard showed why;
the replug published nothing unconfirmed. What it does **not** show: that the
rig's current reads the fans' true current (scale unverified, zero drifts
~25 mA in minutes, and read 0.88 A and 0.20 A for the same three fans in two
boots), or anything about the relays and run switch, which are not fitted.

Still open, next session: whether the zero drift tracks the USB 5 V rail (the
ACS712 is ratiometric; its zero is VCC/2), and whether a divider on OUT into
the ADC's characterised range helps; the in-series meter check; mounting the
MPU-6500 on a fan frame.

### The current investigated with a meter — 24 September, ~15:45 UTC

All three fans spinning, 12 V on. The user's meter, on its 20 V range (so each
reading is limited to 10 mV, which at 185 mV/A is ±0.05 A):

| Measured | Reading |
|---|---|
| ACS712 OUT | 2.52–2.53 V |
| ACS712 VCC | 4.94–5.00 V, so VCC/2 = 2.47–2.50 V |
| True current, from (OUT − VCC/2) / 0.185 V/A | **≈ 0.1–0.25 A**, limited by the meter's resolution |
| ACS712 GND relative to ESP32 GND | **+12 mV** |
| 12 V negative relative to ESP32 GND | **+136 mV** |

What that settles:

- **0.46 A was never the expected current.** It is the sum of the fans'
  *rated maximum* currents, and was quoted in this record and CLAUDE.md as if
  it were their draw. They draw roughly 0.1–0.25 A.
- **The 0.88 A readings were wrong**, by a factor of about four; the later
  ~0.13 A is plausible. The rig's current reading has moved between 0.88 A and
  0.12 A for the same three spinning fans, so no single boot's reading is to
  be believed without a meter beside it.
- **The ground is bad.** The 12 V negative sits 136 mV above the ESP32's
  ground at this current: the pigtail's contact is roughly 0.5–1 Ω, and the fan
  return current flows through it. The ACS712's ground is 12 mV off the ESP32's
  as well — 65 mA of apparent current from the ground alone. A contact that
  moves when touched is consistent with the jump at 15:23:38 (1.26 A spike,
  supply dip to 11.65 V) and with the zero moving 80 mV between boots after
  12 V was first connected.

**RIG_CURRENT remains unverified**, and its readings in the archive for 24
September are not measurements of the fans' current. Nothing in the archive is
changed; this record is where that is said.

No hardware changes tonight. Next session, in order:

1. Move the 12 V negative and the fan returns to a solid joint off the
   breadboard, joined to the ESP32 ground at one point.
2. Add a VCC/2 reference: two 4.7 kΩ from ACS712 VCC to ACS712 GND. OUT minus
   the reference, on the meter's 200 mV range, gives the true current to about
   ±1 mA, and is ratiometric, so it cancels the 5 V rail.
3. Recalibrate the firmware's current against that, and only then consider
   RIG_CURRENT a measurement.

Two cautions for that plan, both from the readings above:

- **OUT minus VCC/2 is not yet the current.** The ACS712's datasheet allows
  its zero-current output to sit up to about ±40 mV from VCC/2 — ±0.2 A. So the
  "≈ 0.1–0.25 A" above carries that uncertainty too, and the reference method
  needs its own no-load reading: (OUT − ref) with 12 V off, subtracted from
  (OUT − ref) with the fans running.
- **The sign is open again.** On the meter, OUT (2.52–2.53 V) sits *above*
  VCC/2 (2.47–2.50 V), which would make the fans' current push OUT up —
  `ACS712_SIGN = +1`, not the −1 set at 15:17. The −1 was inferred from the
  reading going negative the moment 12 V was connected, and a ground that
  shifts by 136 mV when the fans run could produce that on its own. The change
  in (OUT − ref) between fans off and fans on, after the rewire, decides it.

A `drift` diagnostic line was added to the firmware for the no-load drift
test that was to follow the rewire; it was never flashed, and was removed
when the decision below was taken.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | |
| Witnessed by | | |

### Decision — 24 September 2026: the current stays uncalibrated

The user decided not to rewire the bench. **RIG_CURRENT is treated as
permanently unverified.** The plan above (off-breadboard ground joint, VCC/2
reference, recalibration) is withdrawn, not deferred, and so is the drift
investigation. What was done instead:

- **The bridge publishes RIG_CURRENT as UncertainSensorCalibration**
  (0x420A0000), with its value, whenever the firmware reads it successfully —
  never Good. A failed read is still Bad. `sim/test_bridge.py` asserts both,
  end to end through a real OPC UA server.
- **The tag carries a `quality_note`**, "uncalibrated - bench demo only"
  (migration 017, set from `config/unit1_tags.json`, audited). The engine puts
  it first in the tag's health detail; the dashboard's Health tab shows the
  state as `uncertain` in amber, with the note.
- **Nothing computes from it.** No KPI, alert or event template used it on 24
  September, and `engine/test_quality_note.py` now fails if any KPI input,
  event trigger or alert subject resolves to a tag with a quality note.
- Samples archived before this change stay as they were — including the Good
  0.88 A readings; this record is where they are said to be wrong.

Seen live after the change: the Health tab showed RIG_CURRENT with the note.
It showed the state **bad**, not uncertain, because the ESP32 had restarted at
about 15:50 UTC with 12 V already on — probably while the meter probes were on
the board — and so, correctly, refused to take its zero. The `uncertain`
state appears the next time the rig starts USB first, then 12 V.
