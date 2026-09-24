"""The Inspection and Test Plan: one row per test.

Every test names the clause it exists for, the numeric acceptance criterion,
how it is verified -- and, for every automated test, what change to the system
would make it fail. The last column exists because of a finding in the review
of 23 September 2026: three tests passed because the design made failure
impossible, not because the behaviour was demonstrated (a gap check on a
counter the collector assigned itself, a scan rate that could not change, an
availability that counted one sample a minute as full coverage). A criterion
that nothing could fail is not a criterion.

Tests that a machine cannot honestly perform are marked as hold or witness
points rather than being automated into a green tick that means nothing.
"""

from __future__ import annotations

from fat import tests as T
from fat.tests import AUTOMATED, HOLD, WITNESS, Test

PLAN: list[Test] = [
    Test("T-01", "§528", "Acquisition to historian latency",
         "Steady acquisition from the DCS simulator into TimescaleDB",
         "Repeatedly measure the age of the newest archived sample, host clock "
         "on both sides",
         "P95 ≤ 5 s over ≥ 1000 samples; any negative observation fails",
         AUTOMATED, T.latency_p95,
         fails_if="the forwarder batches or lingers for more than ~5 s, the "
                  "archive write slows, or the measurement mixes two clocks"),

    Test("T-02", "§528", "24-hour single-tag trend query",
         "Archive holding ≥ 10 million rows",
         "Query one tag over a 24-hour window, five times, take the worst",
         "≤ 5 s, and the query returns rows",
         AUTOMATED, T.trend_query_24h,
         fails_if="the (tag_id, source_ts) index or chunking is lost, or the "
                  "window selects no data"),

    Test("T-03", "§528", "Dashboard refresh",
         "Visualisation connected to the API",
         "Inspect the configured poll interval and measure authenticated API "
         "response time",
         "2–3 s refresh, API answering within the interval",
         AUTOMATED, T.dashboard_refresh,
         fails_if="the UI's poll interval is set outside 2–3 s, or the API "
                  "answers slower than the interval"),

    Test("T-04", "§528", "Availability over the observation window",
         "Continuous acquisition over the observation window",
         "Every sample the simulator published (its own ledger) looked for in "
         "the archive by exact source timestamp; one-minute coverage beside it",
         "≥ 99.5% of published samples archived, and ≥ 99.5% of minutes covered",
         AUTOMATED, T.availability,
         note="The window includes any deliberate outage tests run during it.",
         fails_if="acquisition stops for more than ~18 s in an hour, or more "
                  "than 1 published sample in 200 fails to reach the archive"),

    Test("T-05", "§318, §384", "Bad, Uncertain, out-of-range, frozen: each flagged",
         "Force Bad and Uncertain at the source; force out-of-range, "
         "cross-tag, rate-of-change and frozen on the live system",
         "Read the archived StatusCodes; run engine.quality_demo and check the "
         "verdict each rule actually produced",
         "Each condition flagged with its own StatusCode; a Bad sample carries "
         "no value; computed quality distinguishable from source quality",
         AUTOMATED, T.quality_conditions,
         note="Restarts the simulated start-up (the rate-of-change step) and "
              "takes about three minutes.",
         fails_if="the collector stores a Bad sample with a value or with the "
                  "wrong code, or any quality rule stops flagging its "
                  "condition or flags it with the wrong severity"),

    Test("T-06", "§318", "No Bad sample is ever silently valid",
         "The whole archive",
         "Count Bad samples that carry a value",
         "Zero",
         AUTOMATED, T.never_silently_valid,
         fails_if="any writer -- collector, OMF receiver, backfill -- stores a "
                  "number with a Bad StatusCode"),

    Test("T-07", "§335", "Source timestamp retention",
         "All acquired samples from the simulated unit",
         "Compare source_ts and server_ts across the archive",
         "No row with server_ts ≤ source_ts; none identical; rows with no "
         "server timestamp counted, not compared",
         AUTOMATED, T.source_timestamp_retention,
         fails_if="the collector stamps samples with receipt time, or copies "
                  "one timestamp into the other"),

    Test("T-08", "§319, §382, §648", "Archive outage and ordered recovery",
         "Stop TimescaleDB for three minutes during acquisition",
         "collector.outage_test — ledger of acquired samples against the "
         "archive, the server's NotificationMessage numbering, and the run's "
         "sequence numbers",
         "Zero loss, ascending source_ts on replay, zero duplicates, no "
         "publish or sequence gap",
         HOLD, None,
         note="Runs for ~9 minutes and stops the database. Executed "
              "separately; the result is in fat/records/stage-03-collector.md. "
              "A hold point: the FAT does not proceed until it has passed."),

    Test("T-09", "§455, §649", "Kill the primary collector",
         "Two collectors running; SIGKILL the primary mid start-up",
         "collector.redundancy_demo — every sample the simulator published "
         "across the transition looked for in the archive",
         "Zero missing samples across the transition; the event frame is complete",
         HOLD, None,
         note="Runs for ~7 minutes. Result in fat/records/stage-11-redundancy.md."),

    Test("T-10", "§442", "Compression ratio and reconstruction error",
         "Two-stage compression, in the library and in the live pipeline",
         "collector/test_compression.py, and collector.compression_report "
         "through the collector's own Pipeline against the simulator",
         "Reconstruction error ≤ CompDev on every tag; U1_MS_TEMP > 10:1; "
         "ratio reported per tag",
         AUTOMATED, T.compression,
         note="Compression is per tag and off by default in the running "
              "system, so the archive the other tests read is uncompressed.",
         fails_if="the swinging door's verification step is removed (the "
                  "textbook algorithm is shown exceeding CompDev), or the live "
                  "pipeline stops passing compressed tags through it"),

    Test("T-11", "§472", "Start-up event auto-captured",
         "A cold start-up on the simulator",
         "Detect event frames over the archived history",
         "Frame captured automatically with all six milestones",
         AUTOMATED, T.startup_event,
         fails_if="a milestone trigger or its debounce stops matching the "
                  "start-up, or event detection stops running"),

    Test("T-12", "§317", "Non-intrusiveness",
         "Source CPU and scan work time, collector attached and detached",
         "Alternating windows; source process CPU at microsecond resolution; "
         "the source's own per-scan work time",
         "≤ 0.5% of one core per subscribed tag, and scan work P95 within 10% "
         "(or 1 ms) of detached",
         AUTOMATED, T.non_intrusiveness,
         note="Detaches the collector for two 45 s windows. Do not run during "
              "another test.",
         fails_if="the collector polls instead of subscribing, or subscribes "
                  "much faster than configured -- see the recorded run with an "
                  "aggressive read-only poller in fat/records/stage-14-fat.md"),

    Test("T-13", "§346, §336", "Time source loss",
         "A DataValue with no SourceTimestamp, one with no ServerTimestamp, "
         "and a repeated SourceTimestamp",
         "Drive each through the collector's own handler and pipeline; check "
         "the archive for any row that breaks the rule",
         "Refused and counted; kept with server_ts NULL; absorbed and counted; "
         "no row violates it",
         AUTOMATED, T.time_source_behaviour,
         fails_if="the collector fills a missing SourceTimestamp or "
                  "ServerTimestamp from anything, or numbers a repeated one"),

    Test("T-14", "§303, §315", "No control path",
         "The whole collector package, recursively",
         "AST inspection of every source file, in code and in comments",
         "No write call, method call or node-management call; the detection "
         "rule proven able to fail",
         AUTOMATED, T.no_control_path,
         fails_if="any collector source file calls write_value, call_method, "
                  "add_*/delete_* node management or similar, even in a comment"),

    Test("T-15", "§341, §392", "Adding a unit is configuration",
         "An existing asset model",
         "Add one object to config/asset_model.json and re-seed",
         "A complete unit appears with its tags; no Python edited",
         WITNESS, None,
         note="Witness point. Demonstrated live; result in "
              "fat/records/stage-05-asset-framework.md."),

    Test("T-16", "§469, §470", "Operator can read the display unaided",
         "The visualisation, during an archive outage and recovery",
         "A colleague who has not seen it describes what happened",
         "Describes the outage and the recovery without being told",
         WITNESS, None,
         note="Witness point. Cannot be automated: the criterion is a human "
              "judgement, and the author is the last person whose opinion of "
              "legibility is worth anything."),

    Test("T-17", "§512–518, §651", "Backup and restore to a clean environment",
         "A populated archive",
         "ops/backup.sh then ops/restore.sh into a separate container",
         "Restored row count equals the source; RTO and RPO documented",
         HOLD, None,
         note="Hold point. Result in docs/backup-and-restore.md. Verify by "
              "ROW COUNT: the first version of this procedure reported success "
              "while losing 12% of the archive."),

    Test("T-19", "§503", "Machine-readable export",
         "A populated archive",
         "ops.export over a window, then inspect the archive produced",
         "CSV and JSON with quality on every row; Bad exports an empty value",
         AUTOMATED, T.machine_readable_export,
         fails_if="the export drops the StatusCode column, or writes 0 for a "
                  "Bad sample"),

    Test("T-20", "§433", "Audit trail on configuration change",
         "Configuration changes of every kind",
         "The audit tests, and every row of audit_log",
         "Every change recorded with actor, time, old value, new value and "
         "reason; unchanged values record nothing; the log cannot be edited",
         AUTOMATED, T.audit_trail,
         fails_if="a configuration change is made without an audit row, an "
                  "audit row lacks its old value, or audit_log accepts an "
                  "UPDATE or DELETE"),
]


# Tests taken out of the plan, and why. Kept here so the ITP and the report say
# a clause is no longer tested, rather than the clause quietly disappearing.
WITHDRAWN: list[tuple[str, str, str, str]] = [
    ("T-18", "§509", "Role-based access",
     "Withdrawn 24 Sep 2026. Token sign-in and role-based access were removed "
     "from the API and the UI by decision, with api/auth.py and "
     "api/test_auth.py. §509 is not demonstrated: anyone who can reach the "
     "API reads everything it serves."),
]
