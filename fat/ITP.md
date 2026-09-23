# Inspection and Test Plan

Orianode Technologies · CRPMS Demonstrator · for KPCL

**Generated from `fat/plan.py`.** Do not edit by hand: the runner executes this same list, and a hand-edited copy would describe tests that are not the ones being run.

Generated 2026-09-23T03:40:59+00:00.

## Summary

| | |
|---|---|
| Tests | 20 |
| Automated | 15 |
| Hold points | 3 |
| Witness points | 2 |

A hold point stops the FAT until it is signed off. A witness point must be observed by a person. Neither is automated, deliberately: turning either into a green tick would make the report shorter and worth less.

## The plan

| Ref | Clause | Test | Condition | Method | Acceptance criterion | Type | Actual | Result | Signature |
|---|---|---|---|---|---|---|---|---|---|
| T-01 | §528 | Acquisition to historian latency | Steady acquisition from the DCS simulator into TimescaleDB | Repeatedly measure the age of the newest archived sample, host clock on both sides | **P95 ≤ 5 s over ≥ 1000 samples; any negative observation fails** | automated | | | |
| T-02 | §528 | 24-hour single-tag trend query | Archive holding ≥ 10 million rows | Query one tag over a 24-hour window, five times, take the worst | **≤ 5 s, and the query returns rows** | automated | | | |
| T-03 | §528 | Dashboard refresh | Visualisation connected to the API | Inspect the configured poll interval and measure authenticated API response time | **2–3 s refresh, API answering within the interval** | automated | | | |
| T-04 | §528 | Availability over the observation window | Continuous acquisition over the observation window | Every sample the simulator published (its own ledger) looked for in the archive by exact source timestamp; one-minute coverage beside it | **≥ 99.5% of published samples archived, and ≥ 99.5% of minutes covered** | automated | | | |
| T-05 | §318, §384 | Bad, Uncertain, out-of-range, frozen: each flagged | Force Bad and Uncertain at the source; force out-of-range, cross-tag, rate-of-change and frozen on the live system | Read the archived StatusCodes; run engine.quality_demo and check the verdict each rule actually produced | **Each condition flagged with its own StatusCode; a Bad sample carries no value; computed quality distinguishable from source quality** | automated | | | |
| T-06 | §318 | No Bad sample is ever silently valid | The whole archive | Count Bad samples that carry a value | **Zero** | automated | | | |
| T-07 | §335 | Source timestamp retention | All acquired samples from the simulated unit | Compare source_ts and server_ts across the archive | **No row with server_ts ≤ source_ts; none identical; rows with no server timestamp counted, not compared** | automated | | | |
| T-08 | §319, §382, §648 | Archive outage and ordered recovery | Stop TimescaleDB for three minutes during acquisition | collector.outage_test — ledger of acquired samples against the archive, the server's NotificationMessage numbering, and the run's sequence numbers | **Zero loss, ascending source_ts on replay, zero duplicates, no publish or sequence gap** | hold | | | |
| T-09 | §455, §649 | Kill the primary collector | Two collectors running; SIGKILL the primary mid start-up | collector.redundancy_demo — every sample the simulator published across the transition looked for in the archive | **Zero missing samples across the transition; the event frame is complete** | hold | | | |
| T-10 | §442 | Compression ratio and reconstruction error | Two-stage compression, in the library and in the live pipeline | collector/test_compression.py, and collector.compression_report through the collector's own Pipeline against the simulator | **Reconstruction error ≤ CompDev on every tag; U1_MS_TEMP > 10:1; ratio reported per tag** | automated | | | |
| T-11 | §472 | Start-up event auto-captured | A cold start-up on the simulator | Detect event frames over the archived history | **Frame captured automatically with all six milestones** | automated | | | |
| T-12 | §317 | Non-intrusiveness | Source CPU and scan work time, collector attached and detached | Alternating windows; source process CPU at microsecond resolution; the source's own per-scan work time | **≤ 0.5% of one core per subscribed tag, and scan work P95 within 10% (or 1 ms) of detached** | automated | | | |
| T-13 | §346, §336 | Time source loss | A DataValue with no SourceTimestamp, one with no ServerTimestamp, and a repeated SourceTimestamp | Drive each through the collector's own handler and pipeline; check the archive for any row that breaks the rule | **Refused and counted; kept with server_ts NULL; absorbed and counted; no row violates it** | automated | | | |
| T-14 | §303, §315 | No control path | The whole collector package, recursively | AST inspection of every source file, in code and in comments | **No write call, method call or node-management call; the detection rule proven able to fail** | automated | | | |
| T-15 | §341, §392 | Adding a unit is configuration | An existing asset model | Add one object to config/asset_model.json and re-seed | **A complete unit appears with its tags; no Python edited** | witness | | | |
| T-16 | §469, §470 | Operator can read the display unaided | The visualisation, during an archive outage and recovery | A colleague who has not seen it describes what happened | **Describes the outage and the recovery without being told** | witness | | | |
| T-17 | §512–518, §651 | Backup and restore to a clean environment | A populated archive | ops/backup.sh then ops/restore.sh into a separate container | **Restored row count equals the source; RTO and RPO documented** | hold | | | |
| T-18 | §509 | Role-based access | The running API, six roles, permissions as data | api/test_auth.py against the live API, plus the role and token tests | **No token 401 on every route; another station 403; unmapped data refused to a station; undeclared route refused; tokens stored hashed** | automated | | | |
| T-19 | §503 | Machine-readable export | A populated archive | ops.export over a window, then inspect the archive produced | **CSV and JSON with quality on every row; Bad exports an empty value** | automated | | | |
| T-20 | §433 | Audit trail on configuration change | Configuration changes of every kind | The audit tests, and every row of audit_log | **Every change recorded with actor, time, old value, new value and reason; unchanged values record nothing; the log cannot be edited** | automated | | | |

## What would make each automated test fail

Every automated criterion must be one the system could fail. A test with no answer here passes because of how it was written, not because of what the system does.

| Ref | Test | Fails if |
|---|---|---|
| T-01 | Acquisition to historian latency | the forwarder batches or lingers for more than ~5 s, the archive write slows, or the measurement mixes two clocks |
| T-02 | 24-hour single-tag trend query | the (tag_id, source_ts) index or chunking is lost, or the window selects no data |
| T-03 | Dashboard refresh | the UI's poll interval is set outside 2–3 s, or the API answers slower than the interval |
| T-04 | Availability over the observation window | acquisition stops for more than ~18 s in an hour, or more than 1 published sample in 200 fails to reach the archive |
| T-05 | Bad, Uncertain, out-of-range, frozen: each flagged | the collector stores a Bad sample with a value or with the wrong code, or any quality rule stops flagging its condition or flags it with the wrong severity |
| T-06 | No Bad sample is ever silently valid | any writer -- collector, OMF receiver, backfill -- stores a number with a Bad StatusCode |
| T-07 | Source timestamp retention | the collector stamps samples with receipt time, or copies one timestamp into the other |
| T-10 | Compression ratio and reconstruction error | the swinging door's verification step is removed (the textbook algorithm is shown exceeding CompDev), or the live pipeline stops passing compressed tags through it |
| T-11 | Start-up event auto-captured | a milestone trigger or its debounce stops matching the start-up, or event detection stops running |
| T-12 | Non-intrusiveness | the collector polls instead of subscribing, or subscribes much faster than configured -- see the recorded run with an aggressive read-only poller in fat/records/stage-14-fat.md |
| T-13 | Time source loss | the collector fills a missing SourceTimestamp or ServerTimestamp from anything, or numbers a repeated one |
| T-14 | No control path | any collector source file calls write_value, call_method, add_*/delete_* node management or similar, even in a comment |
| T-18 | Role-based access | any route loses its principal dependency, the station scope is not applied, or a route is added without a declared permission |
| T-19 | Machine-readable export | the export drops the StatusCode column, or writes 0 for a Bad sample |
| T-20 | Audit trail on configuration change | a configuration change is made without an audit row, an audit row lacks its old value, or audit_log accepts an UPDATE or DELETE |

## Notes

- **T-04** — The window includes any deliberate outage tests run during it.
- **T-05** — Restarts the simulated start-up (the rate-of-change step) and takes about three minutes.
- **T-08** — Runs for ~9 minutes and stops the database. Executed separately; the result is in fat/records/stage-03-collector.md. A hold point: the FAT does not proceed until it has passed.
- **T-09** — Runs for ~7 minutes. Result in fat/records/stage-11-redundancy.md.
- **T-10** — Compression is per tag and off by default in the running system, so the archive the other tests read is uncompressed.
- **T-12** — Detaches the collector for two 45 s windows. Do not run during another test.
- **T-15** — Witness point. Demonstrated live; result in fat/records/stage-05-asset-framework.md.
- **T-16** — Witness point. Cannot be automated: the criterion is a human judgement, and the author is the last person whose opinion of legibility is worth anything.
- **T-17** — Hold point. Result in docs/backup-and-restore.md. Verify by ROW COUNT: the first version of this procedure reported success while losing 12% of the archive.

## Clause coverage

| Clause | Tests |
|---|---|
| §303 | T-14 |
| §315 | T-14 |
| §317 | T-12 |
| §318 | T-05, T-06 |
| §319 | T-08 |
| §335 | T-07 |
| §336 | T-13 |
| §341 | T-15 |
| §346 | T-13 |
| §382 | T-08 |
| §384 | T-05 |
| §392 | T-15 |
| §433 | T-20 |
| §442 | T-10 |
| §455 | T-09 |
| §469 | T-16 |
| §470 | T-16 |
| §472 | T-11 |
| §503 | T-19 |
| §509 | T-18 |
| §512–518 | T-17 |
| §528 | T-01, T-02, T-03, T-04 |
| §648 | T-08 |
| §649 | T-09 |
| §651 | T-17 |

## Sign-off

| Role | Name | Signature | Date |
|---|---|---|---|
| Prepared by | | | |
| Reviewed by | | | |
| Accepted for KPCL | | | |
