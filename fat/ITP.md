# Inspection and Test Plan

Orianode Technologies · CRPMS Demonstrator · for KPCL

**Generated from `fat/plan.py`.** Do not edit by hand: the runner executes this same list, and a hand-edited copy would describe tests that are not the ones being run.

Generated 2026-09-22T19:38:22+00:00.

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
| T-01 | §528 | Acquisition to historian latency | Steady acquisition from the DCS simulator into TimescaleDB | Repeatedly measure the age of the newest archived sample | **P95 ≤ 5 s over ≥ 1000 samples** | automated | | | |
| T-02 | §528 | 24-hour single-tag trend query | Archive holding ≥ 10 million rows | Query one tag over a 24-hour window, five times, take the worst | **≤ 5 s** | automated | | | |
| T-03 | §528 | Dashboard refresh | Visualisation connected to the API | Inspect the configured poll interval and measure API response time | **2–3 s refresh, API answering within the interval** | automated | | | |
| T-04 | §528 | Availability over the observation window | Continuous acquisition over the observation window | Fraction of one-minute buckets containing at least one sample | **≥ 99.5%** | automated | | | |
| T-05 | §318 | Forced Bad and Uncertain are flagged | Force each quality through the simulator's control API | Read the archived sample and its StatusCode | **Each flagged with its own StatusCode; a Bad sample carries no value** | automated | | | |
| T-06 | §318 | No Bad sample is ever silently valid | The whole archive | Count Bad samples that carry a value | **Zero** | automated | | | |
| T-07 | §335 | Source timestamp retention | All acquired samples from the simulated unit | Compare source_ts and server_ts across the archive | **No row with server_ts ≤ source_ts; none identical** | automated | | | |
| T-08 | §319, §382, §648 | Archive outage and ordered recovery | Stop TimescaleDB for three minutes during acquisition | collector.outage_test — ledger of acquired samples against the archive | **Zero loss, ascending source_ts on replay, zero duplicates** | hold | | | |
| T-09 | §455, §649 | Kill the primary collector | Two collectors running; SIGKILL the primary mid start-up | collector.redundancy_demo | **Zero missing samples across the transition; the event frame is complete** | hold | | | |
| T-10 | §442 | Compression ratio and reconstruction error | Two-stage compression over live and synthetic series | collector/test_compression.py plus the live compression report | **Reconstruction error ≤ CompDev; ratio reported per tag** | automated | | | |
| T-11 | §472 | Start-up event auto-captured | A cold start-up on the simulator | Detect event frames over the archived history | **Frame captured automatically with all six milestones** | automated | | | |
| T-12 | §317 | Non-intrusiveness | Source scan rate and CPU, collector attached and detached | Read the simulator's own scan counter and process CPU in both states | **Source scan rate unchanged within 5%** | automated | | | |
| T-13 | §346, §336 | Time source loss | A DataValue with no SourceTimestamp, and a non-advancing timestamp | Inspection of the defined behaviour plus archive consistency checks | **Behaviour defined; no row violates it** | automated | | | |
| T-14 | §303, §315 | No control path | The whole collector package | AST inspection of every source file, in code and in comments | **No write call and no write method; the detection rule proven able to fail** | automated | | | |
| T-15 | §341, §392 | Adding a unit is configuration | An existing asset model | Add one object to config/asset_model.json and re-seed | **A complete unit appears with its tags; no Python edited** | witness | | | |
| T-16 | §469, §470 | Operator can read the display unaided | The visualisation, during an archive outage and recovery | A colleague who has not seen it describes what happened | **Describes the outage and the recovery without being told** | witness | | | |
| T-17 | §512–518, §651 | Backup and restore to a clean environment | A populated archive | ops/backup.sh then ops/restore.sh into a separate container | **Restored row count equals the source; RTO and RPO documented** | hold | | | |
| T-18 | §509 | Role-based access | Six roles with permissions as data | ops/test_ops.py | **Each role carries only its permissions; tokens stored hashed** | automated | | | |
| T-19 | §503 | Machine-readable export | A populated archive | ops.export over a window, then inspect the archive produced | **CSV and JSON with quality on every row; Bad exports an empty value** | automated | | | |
| T-20 | §433 | Audit trail on configuration change | Any configuration change | Inspect audit_log for actor, old value, new value and reason | **Every change recorded with all four** | automated | | | |

## Notes

- **T-04** — The window includes any deliberate outage tests run during it.
- **T-08** — Runs for ~9 minutes and stops the database. Executed separately; the result is in fat/records/stage-03-collector.md. A hold point: the FAT does not proceed until it has passed.
- **T-09** — Runs for ~7 minutes. Result in fat/records/stage-11-redundancy.md.
- **T-12** — Detaches the collector for 20 s. Do not run during another test.
- **T-15** — Witness point. Demonstrated live; result in fat/records/stage-05-asset-framework.md.
- **T-16** — Witness point. Cannot be automated: the criterion is a human judgement, and the author is the last person whose opinion of legibility is worth anything.
- **T-17** — Hold point. Result in docs/backup-and-restore.md. Verify by ROW COUNT: the first version of this procedure reported success while losing 12% of the archive.
- **T-18** — Covered by the automated suite; see fat/records/stage-13-remaining-requirements.md.
- **T-19** — Covered by the automated suite.
- **T-20** — Covered by the automated suite.

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
