# CRPMS Demonstrator

A miniature but architecturally complete power-plant monitoring system, built by
Orianode Technologies ahead of the Karnataka Power Corporation Limited tender for a
Centralised Real-Time Performance Monitoring and Asset Management System.

Every component exists because a numbered clause of that scope of work demands it.
The build proceeds one stage at a time through
[`CRPMS_Demonstrator_Build_Plan.md`](CRPMS_Demonstrator_Build_Plan.md); a stage is
finished when its test has been run and its result recorded in [`fat/`](fat/).

## Status

**Stage 0 — Environment: complete. Test passed 2026-09-22.**

`make up` starts TimescaleDB and `psql` connects: PostgreSQL 16.15 with the
TimescaleDB 2.30.1 extension, server in UTC, reachable both inside the container
and from the host over TCP, on a named volume that survives a restart. The record,
with the output it is based on, is
[`fat/records/stage-00-environment.md`](fat/records/stage-00-environment.md).

Stage 1 has not been started. Apart from the environment itself, nothing in this
repository has yet been measured doing anything.

## Prerequisites

Python 3.11+, Docker, Node 20+.

```bash
make doctor
```

reports which of the three this machine has.

## Quick start

```bash
make install   # create .venv and install the project
make up        # start TimescaleDB, wait until it accepts connections
make psql      # psql shell inside the container
make down      # stop it; the named volume crpms-pgdata is kept
```

`make help` lists every target.

## Layout

| Directory | Contents | Built by |
|---|---|---|
| `sim/` | OPC UA server standing in for a DCS | Stage 1 |
| `archive/` | TimescaleDB schema and numbered migrations | Stage 2 |
| `collector/` | acquisition, buffering, compression, OMF output | Stages 3, 4, 9, 11 |
| `engine/` | asset model, quality rules, KPIs, event frames | Stages 5–8 |
| `firmware/` | ESP32, Modbus, the bench rig | Stage 10 |
| `api/`, `ui/` | FastAPI and the React visualisation | Stage 12 |
| `fat/` | test plan and records | throughout, formalised at Stage 14 |
| `scraper/` | Karnataka SLDC live generation recorder | — |

## What this demonstrator will and will not show

It is intended to demonstrate: real industrial protocols, real sensors, preserved
measurement times, quality carried through calculations, outage and ordered
recovery with zero loss, two-stage compression, a scaling asset model, event frames
with milestone comparison, and PI-compatible OMF output. None of that has been
built yet, let alone measured; each claim becomes true only when its stage's test
passes and the result is in `fat/`.

It does not demonstrate, and will not: any real BHEL, Yokogawa, ABB or Andritz DCS;
anything running on a licensed AVEVA PI installation; 34,700 I/O across 13 sites;
wide-area network behaviour; cross-site time synchronisation; or boiler and turbine
physics.

The second list is what makes the first list believable.
