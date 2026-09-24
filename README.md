# CRPMS Demonstrator

A miniature but architecturally complete power-plant monitoring system, built by
Orianode Technologies ahead of the Karnataka Power Corporation Limited tender
for a Centralised Real-Time Performance Monitoring and Asset Management System.

Every component exists because a numbered clause of that scope of work demands
it. The build proceeded one stage at a time through
[`CRPMS_Demonstrator_Build_Plan.md`](CRPMS_Demonstrator_Build_Plan.md); a stage
is finished when its test has been run and its result recorded in
[`fat/records/`](fat/records/).

## Status

| Stage | | Record |
|---|---|---|
| 0 Environment | **passed** | [record](fat/records/stage-00-environment.md) |
| 1 DCS simulator | **passed** | [record](fat/records/stage-01-dcs-simulator.md) |
| 2 Schema and archive | **passed** | [record](fat/records/stage-02-schema-and-archive.md) |
| 3 The collector | **passed** | [record](fat/records/stage-03-collector.md) |
| 4 Compression | **passed** | [record](fat/records/stage-04-compression.md) |
| 5 Asset framework | **passed** | [record](fat/records/stage-05-asset-framework.md) |
| 6 Quality rules and tag health | **passed** | [record](fat/records/stage-06-quality-rules.md) |
| 7 KPI engine | **passed** | [record](fat/records/stage-07-kpi-engine.md) |
| 8 Event frames | **passed** | [record](fat/records/stage-08-event-frames.md) |
| 9 OMF emitter | **passed** | [record](fat/records/stage-09-omf.md) |
| 10 The hardware rig | **built, test blocked** — needs the physical rig | [record](fat/records/stage-10-hardware-rig.md) |
| 11 Redundancy | **passed** | [record](fat/records/stage-11-redundancy.md) |
| 12 The visualisation | **built** — its criterion is a human judgement | [record](fat/records/stage-12-visualisation.md) |
| 13 Remaining requirements | **passed** | [record](fat/records/stage-13-remaining-requirements.md) |
| 14 The FAT | **automated tests 15 of 15** ([report](fat/reports/FAT-20260923T053731Z.md)); hold and witness points await signature — [plan](fat/ITP.md), [procedure](fat/procedure.md) | [record](fat/records/stage-14-fat.md) |

**A code review on 23 September 2026** found claims the code did not back and
three FAT tests that could not fail. What it found, what was changed and what
was measured afterwards is in
[`fat/records/review-2026-09-23.md`](fat/records/review-2026-09-23.md); the
stage records it affects carry dated corrections. Stages 3 and 11 were re-run
against the changed code and passed, and the FAT was re-run on a clean tree and
passed 15 of 15.

Two things are outstanding and both need a person, not more code:

- **Stage 10** needs the rig run end to end. The firmware has been flashed and
  runs against the real sensors on the bench (24 Sep; relays and run switch not
  yet fitted, 12 V not yet connected), and the whole path from a failed sensor
  to a Bad KPI is demonstrated against a stand-in — but the rig has not yet
  been read through the bridge, and no probe has been unplugged.
- **Stage 12's** criterion is "a colleague who has not seen it can describe what
  happened". Nobody has watched it.

## Quick start

```bash
make doctor          # Python 3.11+, Docker, Node 20+
make install         # .venv with the project
make up              # TimescaleDB
make migrate         # schema
make seed-tags seed-assets seed-quality seed-kpis seed-alerts

make sim             # the OPC UA DCS simulator
make collector       # acquisition
make engine          # KPIs, quality rules, event frames, alerts, capacity
make api             # FastAPI on :8000 — every call needs a token
make token           # a read-only token for the UI, shown once
make ui              # the visualisation on :5173
```

`make help` lists every target.

## The acceptance tests

```bash
make outage-test         # Stage 3: stop the database for 3 minutes
make compression-report  # Stage 4: ratios and reconstruction error
make quality-demo        # Stage 6: force each quality condition
make kpi-demo            # Stage 7: Bad input -> Bad KPI
make events-demo         # Stage 8: two start-ups, compared
make omf-demo            # Stage 9: OMF, switchable endpoint
make redundancy-demo     # Stage 11: kill the primary mid start-up
make fat                 # the automated FAT, writes a report
```

## Layout

| Directory | Contents |
|---|---|
| `sim/` | OPC UA server standing in for a DCS, plus the Modbus bridge |
| `archive/` | schema, numbered migrations, OMF receiver |
| `collector/` | acquisition, buffering, compression, OMF output |
| `engine/` | asset model, quality rules, KPIs, event frames, and the service that runs them |
| `ops/` | access control, alerts, capacity, backup, export |
| `firmware/` | ESP32 bench rig, and a stand-in for testing the bridge |
| `api/`, `ui/` | FastAPI and the React visualisation |
| `fat/` | inspection and test plan, procedure, runner, records, reports |
| `config/` | tags, asset model, quality rules, KPIs, alert rules, event templates, OPC UA sources |
| `docs/` | generated KPI dictionary, backup and restore |
| `scraper/` | Karnataka SLDC live generation recorder |

## What it demonstrates

Real industrial protocols end to end: a real OPC UA server, a real Modbus
fieldbus (TCP, and RTU compiled for RS-485), real OMF. Measurement times
preserved from the source to the historian and into PI-compatible output, and a
missing server timestamp stored as missing. Quality carried as the numeric OPC
UA StatusCode through every layer and into every calculation, with a KPI
returning Bad and naming the input that caused it. An archive outage survived
with zero loss and ordered recovery, checked against the collector's own
ledger, the OPC UA server's message numbering and per-run sequence numbers.
Two-stage compression with the reconstruction bound actually met, in the live
pipeline. An asset model where adding a unit is one line of configuration.
Event frames captured continuously and compared milestone by milestone.
Redundancy with no arbitration, because the schema makes a duplicate
impossible, and zero missing samples across a killed collector checked against
the source's own record of what it published. Every API call authenticated and
scoped by role.

Each of those was measured, and the measurement is in `fat/records/` with the
defects found on the way there.

## What it does not demonstrate

It does not touch a real BHEL, Yokogawa, ABB or Andritz DCS. It is not running
on a licensed AVEVA PI installation, and whether a real PI Web API endpoint
accepts its OMF has not been tested. It is not proven at 34,700 I/O across 13
sites — every figure was measured on one laptop, one simulated unit and a bench
rig that has not yet been built; the firmware compiles and has never run. The
simulated source is asyncua, whose server suppresses a status change inside a
deadband, so this demonstrator runs with no deadband at the source; how a real
DCS behaves there is not measured. It says nothing about wide-area network
behaviour, cross-site time synchronisation, per-station licensing, or OT
security zoning.

Plant physics is limited to definitional ratios from standard utility practice.
Heat rate, auxiliary power and specific coal consumption are implemented;
cylinder efficiency and condenser performance need published steam tables and
are **not implemented**, rather than approximated.

The access control is not an identity system: no password policy, no lockout, no
MFA, no SSO, no token expiry. The simulator's control API is unauthenticated.
The backup has no off-site copy and no encryption at rest. KPIs are computed
live and not recomputed for a period the archive was down.

The second list is what makes the first list believable.
