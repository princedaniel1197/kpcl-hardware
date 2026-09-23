# Stage 13 — The remaining named requirements

| | |
|---|---|
| Stage | 13 |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 13 |
| Date performed | 2026-09-22 |
| **Result** | **PASS** — all six built and exercised; 22 tests |

## 1. Role-based access (§509)

Six roles as the clause names them, with permissions as data.

```
username         role          station   enabled  last seen
pd.engineer      engineering   -         True     never
rtps.operator    station       RTPS      True     never
```

Tokens are stored as SHA-256 and shown once; the plaintext is never persisted,
never logged, and cannot be recovered. An unknown token and a revoked one give
the **same** error message, because telling an attacker which tokens exist is
free information. The `station` role is scoped and refuses to be created
without a station; every other role is fleet-wide.

**This is not an identity system**, and the record says so rather than letting a
reviewer assume otherwise: no password policy, no lockout, no MFA, no SSO, no
session management, no rotation schedule. A real deployment puts authentication
behind the customer's directory. What is demonstrated is that every call carries
a principal, the principal has one role, and the role decides what it may do —
which is the part §509 is about.

## 2. Audit trail (§433)

Already present from migration 005 and used throughout: tag seeding, asset
instantiation, KPI versioning, alert rules, principal creation and revocation,
backups and exports all write actor, timestamp, old value, new value and reason.
Fixed during Stage 7 after it was found naming the wrong field.

## 3. Backup, restore, RPO and RTO (§512–518, §651)

Measured on a 10.15 million row archive:

| | Measured |
|---|---|
| Backup duration | **8 s** |
| Backup size | **154.9 MB** from a 1.1 GB database |
| **RTO** | **20 s**, restored into a clean container and verified |
| Rows restored | **10,155,600 of 10,155,600** |
| **RPO** | **1 hour**, the backup interval |

Full documentation, including what it does *not* do, in
`docs/backup-and-restore.md`.

### The defect worth putting in front of a reviewer

The first restore **reported success, finished in 22 seconds, and silently lost
1.2 million rows — 12% of the archive.**

Two mistakes compounded. TimescaleDB requires a restore to be bracketed by
`timescaledb_pre_restore()` and `timescaledb_post_restore()`, which disable the
chunk-routing machinery for the duration; without them, rows destined for
hypertable chunks are re-routed mid-load and vanish. And the script threw away
`pg_restore`'s stderr with `2>/dev/null || true`, which is why the first mistake
was invisible.

The second is the worse one. A restore that hides its complaints will hide the
next problem too, and a backup nobody has verified is a hope rather than a
control. It was found by comparing row counts between source and restored copy,
which is now part of the procedure and printed by the script.

## 4. Configurable alerts with email (§507)

Eight rules seeded from `config/alert_rules.json`. Running the engine against
the live system:

```
checked 8 rules: 1 raised, 0 cleared, 1 open
OPEN [warning] Bench rig hub probe Bad — RIG_HUB_TEMP has been Bad for 15s
```

**Quality is a first-class alert condition.** A rule can fire on a tag going
Bad, not only on a tag crossing a number. A system that can only alarm on
thresholds cannot tell you your instrument has failed — it will happily report
that a dead transmitter is reading a perfectly normal 0.

Conversely, a **threshold** rule will not fire on a Bad sample: a Bad sample
carries no value, and a threshold rule that fires on one is reporting a number
that does not exist.

Every rule carries a debounce, so a spike does not page anyone.

Email is **off unless `CRPMS_SMTP_HOST` is configured**, and a delivery failure
is recorded on the alert row (`delivered`, `delivery_error`) rather than
swallowed. An alerting system that silently fails to alert is worse than none,
because it is trusted.

## 5. Central capacity monitoring (§344)

```
  database            1.1 GB
  sample hypertable   1.1 GB
  sample rows         10,150,954
  chunks              16
  host disk used      13.4%
  host disk free      802.3 GB
  disk exhausted in   not growing — no projection
```

Growth rate and a projection, not just a level: a capacity figure without a
growth rate answers "how full is it" but not "when does it stop working", and
the second is the question that matters. When growth is zero the projection is
an honest "not applicable" rather than a reassuring large number.

## 6. Machine-readable export (§503)

```
exported to exports/crpms-export-20260922T192530Z.zip (1.4 MB)
  samples.csv             7,517,281 bytes  sha256 f0e789bc17d70e77
  kpi_values.csv                261 bytes  sha256 14ddb9c7566d04af
  configuration.json         54,133 bytes  sha256 ac68c5d0e6db49a5
  event_frames.csv            4,089 bytes  sha256 724646ae770a5bba
  audit_log.csv              12,185 bytes  sha256 34137b8fb4ede3bb
```

CSV for series, JSON for configuration, a manifest with SHA-256 per file and the
window and actor, so a file found later can be traced back.

**The export carries quality**, and a Bad sample exports an **empty** value
field. Verified on the real export:

```
72,107 sample rows: 72,043 Good, 64 Bad
a Bad row: tag=RIG_HUB_TEMP value='' quality=2156593152 class=Bad
confirmed: Bad samples export an EMPTY value field, not a zero
```

An export that drops quality hands the recipient a file that looks authoritative
and cannot be checked — and once it has left this system, nobody can ever
recover which readings were trustworthy. `kpi_values.csv` carries the definition
version and the equation as it stood.

## A defect in the tests, corrected

The 22 tests passed in isolation and then **failed the second time the suite
ran**. `create_principal` commits — it has to, because the token is returned to
a caller who will use it on a different connection — so rolling the fixture back
left the test principals behind, and the next run collided on the unique
username.

That is the worst kind of test: green on the machine that wrote it, red on the
next one. The fixture now removes what the tests create, and the suite was run
twice in a row to prove it.

Worth recording because the first commit of this stage claimed "223 passed" on
the strength of a single isolated run. The claim was wrong when it was made.

## Addendum — 23 September 2026

- **§509: "every call carries a principal" was not true of the API.** The
  access library existed and nothing called it; every route was
  unauthenticated. It is now enforced on every route and the WebSocket, with
  the station scope applied (`api/auth.py`), and T-18 tests it against the live
  API.
- **§433: not every change was audited, and the log could be edited.** The
  Stage 6 demonstration changed an EURange without an audit row; the quality
  and alert seeders recorded "old value NULL" when overwriting; tag-to-element
  mapping was unaudited; the access-control test fixture deleted audit rows.
  All fixed; `audit_log` is append-only in the database (migration 015).
- **Backup:** a dump that failed half way was left under its final name and
  counted towards retention; a failed restore exited 0. Both fixed. Re-verified
  on 23 September into a clean container: **10,921,597 of 10,921,597 samples**
  and 336 of 336 audit rows; backup 12 s (167.9 MB), RTO 36 s.
- The export now carries each sample's collector run and sequence number, and
  the source configuration.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
