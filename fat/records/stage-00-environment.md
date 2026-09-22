# Stage 0 — Environment

| | |
|---|---|
| Stage | 0 — Environment |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 0 |
| Acceptance criterion | `make up` starts TimescaleDB and `psql` connects |
| Date performed | 2026-09-22 |
| Host | macOS (Darwin 25.6.0), Apple silicon (arm64) |
| **Result** | **PASS** |

## Environment as tested

`make doctor`:

```
  OK    python 3.11+   Python 3.11.0 (/usr/local/bin/python3)
  OK    docker         Docker version 29.8.0, build 88096ef
        note: using the CLI inside Docker.app; no /usr/local/bin/docker symlink yet
  OK    docker daemon   running
  OK    node           v24.19.0
```

Docker client and server both 29.8.0. Docker Desktop's CLI has not been symlinked
into `/usr/local/bin`; the Makefile uses the binary inside the application bundle
and puts that bundle's `bin` directory on PATH, so no manual step is required.

## Test 1 — `make up` starts TimescaleDB

```
 Image timescale/timescaledb:latest-pg16 Pulled
 Volume crpms-pgdata Created
 Network orianode-crpms_default Created
 Container crpms-timescaledb Started
waiting for TimescaleDB . ready
TimescaleDB ready on port 5432
```

Container state:

```
NAMES               IMAGE                               STATUS                  PORTS
crpms-timescaledb   timescale/timescaledb:latest-pg16   Up 6 seconds (healthy)  0.0.0.0:5432->5432/tcp
```

**PASS** — the compose healthcheck reports healthy; the `wait-db` target returned
ready on the first poll after container start.

## Test 2 — `psql` connects

```
$ make psql   (docker exec crpms-timescaledb psql -U crpms -d crpms)

 PostgreSQL 16.15 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit

   extname   | extversion
-------------+------------
 timescaledb | 2.30.1

 TimeZone
----------
 UTC

              now
-------------------------------
 2026-09-22 15:18:16.458166+00
```

**PASS.** PostgreSQL 16.15, TimescaleDB extension 2.30.1 present, server timezone
UTC as configured. The image is native `aarch64` — no emulation layer is in the
path between this project and its database.

## Test 3 — host-to-container connection over TCP (psycopg)

The acceptance criterion is satisfied by Test 2, but `psql` there runs *inside* the
container. Stage 3 onward connects from the host over TCP, which is a different
path and is therefore tested separately.

```
python  : 3.11.0 arm64
psycopg : 3.3.6
host->container over TCP: ('crpms', 'crpms', IPv4Address('172.18.0.2'), 5432)
timescaledb extension   : 2.30.1
server timezone         : UTC
```

**PASS** — `postgresql://crpms:crpms@localhost:5432/crpms` connects from the host
virtualenv.

## Test 4 — the named volume survives a restart

Not named in the acceptance criterion, but the archive is the point of this stage:
a volume that does not persist would not be discovered until samples were lost.

| Step | Result |
|---|---|
| Write a probe row, `make down`, `make up`, read it back | row returned unchanged |

**PASS** — `crpms-pgdata` persists across a `down`/`up` cycle. Probe table dropped
afterwards; the database is left clean.

## Test 5 — `make install` builds the environment

`make install` created `.venv` on Python 3.11.0 and installed the project editable
with the Stage 0 dependency set:

| Package | Installed |
|---|---|
| asyncua | 2.0.1 |
| pymodbus | 3.15.0 |
| psycopg (+binary) | 3.3.6 |
| fastapi | 0.141.1 |
| uvicorn | 0.53.0 |
| pydantic | 2.13.5 |
| numpy | 2.4.6 |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 |

**PASS.**

## Observation carried to Stage 1

`asyncua` resolved to **2.0.1**. The build plan and this project's dependency floor
were written against the 1.x series. The 2.x server API has not been checked for
compatibility with what Stage 1 requires — in particular setting `SourceTimestamp`
and `ServerTimestamp` independently on a `ua.DataValue`. This is an observation,
not a defect: it is verified or corrected at Stage 1, and the version is recorded
here so that any later behaviour traces to a known set.

## Changes made to the Stage 0 deliverables while testing

| Change | Reason |
|---|---|
| Makefile falls back to the Docker CLI inside `Docker.app` and puts its `bin` directory on PATH | Docker Desktop had not created `/usr/local/bin/docker`; `docker` also shells out to `docker-credential-desktop`, so the whole directory is needed, not one binary |
| Daemon check changed from `docker info` to `docker ps` | `docker info` exits 0 even when the server is unreachable, reporting a daemon that is not there — a check that can return a false pass is worse than no check |

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
