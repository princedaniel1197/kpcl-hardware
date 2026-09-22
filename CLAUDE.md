# CRPMS Demonstrator — project context

Read this before doing anything in this repository. It is not background; it contains
rules that override your defaults.

## What this is

A miniature but architecturally complete power-plant monitoring system, built by
Orianode Technologies to demonstrate competence ahead of the Karnataka Power Corporation
Limited (KPCL) tender for a Centralised Real-Time Performance Monitoring and Asset
Management System — 13 stations, 58 units, 7,369.7 MW, ~34,700 I/O, a 50,000-tag historian.

Every component here exists because a numbered clause of that tender demands it. Clause
references appear throughout `CRPMS_Demonstrator_Build_Plan.md` and in code comments. When
a design decision is in question, the clause decides it, not convenience.

This is a demonstrator. It is honest about being one. It is not a mock-up: every behaviour
it claims must actually happen, measurably, under test.

## The rules that are not negotiable

These are the differences between a system built by people who understand operational
technology and people who do not. Do not optimise them away, do not simplify them for a
first pass, and do not defer them to "later".

1. **Measurement time is not arrival time.** Every sample carries both a `SourceTimestamp`
   (when the value was produced) and a `ServerTimestamp` (when it went on the wire). They
   are different values. Never substitute receipt time for a valid SourceTimestamp, in any
   code path, including recovery and backfill. (§335)

2. **Quality is first-class.** Quality is the numeric OPC UA StatusCode, never a boolean or
   a string. It travels with the value through every layer and into every calculation. A
   value that arrived Good but failed a range check is a different thing from a value that
   arrived Bad, and the system must always be able to say which. (§318, §384)

3. **Never silently substitute.** A calculation with a Bad input returns Bad, with a reason
   naming the offending input. It does not return zero, the last good value, a default, or
   an interpolation. This rule has no exceptions. (§318)

4. **The acquisition path is read-only.** There is no write method anywhere in the collector
   package. Not commented out, not behind a flag, not unused. A test asserts its absence.
   (§303, §315)

5. **Losing the uplink must not stop acquisition.** Buffer locally, keep reading. On
   restore, replay in ascending source-timestamp order, preserving the original timestamps
   and quality, through an upsert that cannot duplicate. (§319, §382, §648)

6. **Idempotency is structural, not procedural.** The primary key on `sample` is
   `(tag_id, source_ts)`. Replay can never create a duplicate because the schema forbids it.

7. **Adding an asset is configuration, not code.** A second unit is a new element created
   from an existing template. If adding a unit requires editing Python, the asset model is
   wrong. (§341, §392)

## Stack

Python 3.11+, asyncio throughout. FastAPI (no other web framework). TimescaleDB in Docker.
React with React Flow for the pipeline view. ESP32 firmware in C++ using eModbus.

Libraries: `asyncua`, `pymodbus`, `psycopg[binary]`, `fastapi`, `uvicorn`, `pydantic`, `numpy`.

## Layout

```
sim/          OPC UA server standing in for a DCS
collector/    acquisition, buffering, compression
archive/      schema, migrations, OMF receiver
engine/       quality rules, KPIs, event frames, asset model
api/          FastAPI + WebSocket
ui/           React visualisation
fat/          test plan and records
firmware/     ESP32
scraper/      Karnataka SLDC live generation recorder
```

## Conventions

- Tag and element naming follows ISA-95 / ISA-88 principles; the convention is documented
  in `engine/README.md` and is not changed ad hoc.
- Quality stored as the numeric StatusCode; a helper view decodes it for humans.
- Every KPI definition is versioned, and every computed value records the version that
  produced it, so any historical result traces to the exact equation used. (§320, §379)
- Configuration changes are written to `audit_log` with actor, timestamp, old value, new
  value and reason. (§433)

## How work proceeds

One stage at a time, from `CRPMS_Demonstrator_Build_Plan.md`. Each stage has a test. **Do
not begin a stage until the previous stage's test has actually been run and passed.** A
stage that looks finished but has not passed its test is not finished.

When a stage's test passes, record the result in `fat/` — those records become the test
report, which is the deliverable that persuades.

## What not to do

- Do not replace the OPC UA simulator with mock data or a test fixture. Everything
  downstream must talk to a real OPC UA server, because that is the point.
- Do not build the visualisation early. It hangs off real events from a working system.
- Do not add a control or write path "for testing".
- Do not smooth over a Bad value to make a chart look nicer.
- Do not invent plant physics. Heat rate, cylinder efficiency and condenser calculations
  use published steam tables and documented equations, or they are not implemented. (§486)
- Do not claim, in code comments, docs or the README, anything the system has not been
  measured doing.

## The hardware rig

Real sensors on a bench, read by an ESP32 publishing Modbus. Available:

| Item | Qty | Role |
|---|---|---|
| ESP32 38-pin NodeMCU | 2 | Field device / Modbus server |
| 12 V 0.18 A fan | 2 | The machine being monitored |
| 12 V 4010 fan | 1 | Second switchable load |
| ACS712 5 A current sensor | 1 | Load current, ~0.46 A total, 185 mV/A |
| ACS712 30 A current sensor | 1 | Spare — too coarse for this load |
| DS18B20 probe | 2 | Motor hub temperature, ambient |
| MPU-6050 | 1 | Vibration, and coast-down detection |
| 1-channel 5 V relay | 2 | ESP32-commanded start/stop, two groups |
| MAX485 module | 2 | RS-485 two-wire bus |
| USB-CH340 RS-485 adapter | 1 | Bus master on the laptop |
| Rocker switch, tactile buttons | — | Digital state inputs |

Wiring notes that matter: all three fans sit downstream of the ACS712 so it measures total
load. Fan starts are staggered by ~500 ms in firmware — three fans starting together draw
about 1.2 A against a 1 A supply. The DS18B20 data line needs a 4.7 kΩ pull-up to 3.3 V,
and both probes share one wire by address.

The station-side machine is a spare laptop, not a Pi. Pulling its Ethernet cable is the
outage test.

## Honesty

When describing what this system does — in the README, in the FAT report, or anywhere else
— state both halves.

It demonstrates: real industrial protocols, real sensors, preserved measurement times,
quality carried through calculations, outage and ordered recovery with zero loss, two-stage
compression, a scaling asset model, event frames with milestone comparison, and PI-compatible
OMF output.

It does not demonstrate: any real BHEL, Yokogawa, ABB or Andritz DCS; anything running on a
licensed AVEVA PI installation; 34,700 I/O across 13 sites; wide-area network behaviour;
cross-site time synchronisation; or boiler and turbine physics.

The second list is what makes the first list believable.

## Status

Stage: 6 — COMPLETE, test passed 2026-09-22 (`fat/records/stage-06-quality-rules.md`).
Stages 0 to 5 complete; records in `fat/records/`. Stage 7 has not been begun.
Update this line as stages complete.
