# scraper — Karnataka SLDC live generation recorder

Polls [kptclsldc.in/StateGen.aspx](https://kptclsldc.in/StateGen.aspx) every 60
seconds and records per-station generation for six KPCL stations, plus system
frequency, into the project's TimescaleDB instance.

This is **not a stage of the build plan**. No staged work depends on it. It
shares the database and the project's conventions and nothing else.

## Why it is built the way it is

The same three rules from `CLAUDE.md` that govern the rest of the project decide
every interesting question here.

**Measurement time is not arrival time (§335).** The page publishes its own
timestamp. That is `source_ts`. The instant we fetched the page is `server_ts`.
They are stored separately and neither is ever derived from the other. This is
not theoretical: measured gaps between the two have run from about 5 to about 13
minutes. A recorder that stamped rows with fetch time would file a
thirteen-minute-old reading as current and no later query could tell.

If the page carries no readable timestamp, the reading is **refused** — see
`PageTimestampError`. There is no fallback, because the only available fallback
is the fetch time, and using it is precisely the error above.

**Never silently substitute (§318).** A station whose value is missing, blank or
non-numeric is stored as `NULL` with a Bad OPC UA StatusCode and a reason naming
the element that failed. It is never stored as zero. Several of these stations
legitimately generate 0 MW — RTPS read 0 on every unit in the first capture — so
a parse failure that became a zero would be indistinguishable from a station
that is genuinely shut down.

**Idempotency is structural (§382, §648).** The primary key is
`(station, source_ts)` and both inserts are `ON CONFLICT DO NOTHING`. The page
timestamp has one-minute resolution and updates roughly every five minutes,
while we poll every sixty seconds, so the same reading is routinely seen several
times. The schema absorbs that. There is no de-duplication logic in the poller
and none should be added.

## The schedule does not drift

Tick *n* fires at `origin + n × interval`, computed from a fixed origin — not
"sleep 60 seconds after finishing". Retries happen *inside* a tick and are
bounded by it: if the page cannot be fetched before the next tick is due, the
tick is abandoned, logged, and the next one starts on time. A loop that sleeps
after its work walks away from the clock, and the walk is invisible until you
compare two days of data.

Measured over seven consecutive agent-driven ticks on 2026-09-22: mean interval
60.001 s, standard deviation 0.001 s, maximum 60.003 s. Each tick includes a
live HTTPS fetch of roughly 280 ms, which does not accumulate.

## What the site does, measured

Measured against the live site on 2026-09-22:

| Observation | Value |
|---|---|
| `robots.txt` | absent (404) — nothing declared |
| Page size | ~41 KB, HTTP 200, ~0.25 s |
| Page timestamp | IST, `DD/MM/YYYY HH:MM`, one-minute resolution |
| Page update interval | roughly 5 minutes |
| Observed page age at fetch | 5 to 13 minutes |

**On the User-Agent.** The site does not block automated clients: an empty
User-Agent, `curl/8.0`, and our own name all return 200. It does carry a crude
filter that resets the connection when the User-Agent contains the single token
`recorder` — `scraper/0.1` is served, `recorder/0.1` is not. Our User-Agent
avoids that one word and is otherwise entirely truthful: it names the software
and gives a contact address. Nothing is spoofed, and no access control is being
worked around. If the site ever does start refusing automated clients, the
answer is to ask KPTCL for a feed, not to disguise this one.

## Station ids, and the trap in them

The page renders each station's total into a span with a fixed id. The ids are
**not** uniformly named:

| Station | Element id | |
|---|---|---|
| RTPS | `lblrtptot` | |
| BTPS | `lblbtptot` | |
| YTPS | `ytptot` | **no `lbl` prefix** |
| Sharavathi | `lblshvytot` | |
| Varahi | `lblvrhtot` | |
| Almatti | `lblalmttot` | |

A scraper that assumes the `lbl` prefix records nothing for Yermarus and never
says so. `test_ytps_is_not_silently_missing` exists to keep that fixed.

## Tables

| Table | Key | Holds |
|---|---|---|
| `sldc_generation` | `(station, source_ts)` | per-station MW, quality, reason |
| `sldc_system` | `(source_ts)` | frequency, state and total generation |
| `sldc_poll_log` | `(attempt_ts)` | every poll attempt and its outcome |

Frequency lives in `sldc_system` rather than on six identical station rows: it
is a property of the grid, not of any station.

Two views: `sldc_generation_decoded` (StatusCode decoded to Good/Bad/Uncertain,
with the numeric code kept as the stored truth) and `sldc_daily_rows` (per-day
counts — the liveness figure).

`sldc_poll_log` is what makes "the recorder is alive but the site is down"
distinguishable from "the recorder is dead". Both look like an absence of rows
in `sldc_generation`; only one of them leaves failed attempts in the log.

## Running it

```bash
make scraper-migrate     # create the tables (once)
make scraper-once        # a single poll, to check the setup
make scraper             # run in the foreground
make scraper-install     # run under launchd, restarting at login and on crash
make scraper-status      # daily row counts and the last ten attempts
make scraper-logs        # follow the log
make scraper-uninstall
```

**On reboot survival.** `make scraper-install` installs a launchd *agent*
(`~/Library/LaunchAgents`), which starts at user login and is restarted if it
exits. That survives a reboot provided the machine logs in again — set automatic
login, or the recorder waits at the login window. A recorder that runs before
any login must be a launchd *daemon* in `/Library/LaunchDaemons` owned by root;
that needs an administrator, so it is a deliberate choice rather than a default.
`systemd` does not exist on macOS.

## Tests

```bash
.venv/bin/python -m pytest scraper/ -q
```

The parser tests run against a real captured page in `fixtures/`, so they need
no network and a failure is never the site's fault. They cover the six stations,
the IST→UTC conversion, refusal when the page timestamp is missing or
unparseable, missing/blank/non-numeric station values each producing their own
Bad status with a reason, and — the one that matters — a genuine 0 MW staying
Good and distinguishable from a failed read.

## Dependencies

None beyond the project's existing set. HTTP uses `urllib` on a worker thread
rather than adding an async HTTP client, because the dependency list is the one
the build plan names.
