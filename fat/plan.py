"""The Inspection and Test Plan: one row per test.

Every test names the clause it exists for, the numeric acceptance criterion, and
how it is verified. Tests that a machine cannot honestly perform are marked as
hold or witness points rather than being automated into a green tick that means
nothing.
"""

from __future__ import annotations

from fat import tests as T
from fat.tests import AUTOMATED, HOLD, WITNESS, Test

PLAN: list[Test] = [
    Test("T-01", "§528", "Acquisition to historian latency",
         "Steady acquisition from the DCS simulator into TimescaleDB",
         "Repeatedly measure the age of the newest archived sample",
         "P95 ≤ 5 s over ≥ 1000 samples",
         AUTOMATED, T.latency_p95),

    Test("T-02", "§528", "24-hour single-tag trend query",
         "Archive holding ≥ 10 million rows",
         "Query one tag over a 24-hour window, five times, take the worst",
         "≤ 5 s",
         AUTOMATED, T.trend_query_24h),

    Test("T-03", "§528", "Dashboard refresh",
         "Visualisation connected to the API",
         "Inspect the configured poll interval and measure API response time",
         "2–3 s refresh, API answering within the interval",
         AUTOMATED, T.dashboard_refresh),

    Test("T-04", "§528", "Availability over the observation window",
         "Continuous acquisition over the observation window",
         "Fraction of one-minute buckets containing at least one sample",
         "≥ 99.5%",
         AUTOMATED, T.availability,
         note="The window includes any deliberate outage tests run during it."),

    Test("T-05", "§318", "Forced Bad and Uncertain are flagged",
         "Force each quality through the simulator's control API",
         "Read the archived sample and its StatusCode",
         "Each flagged with its own StatusCode; a Bad sample carries no value",
         AUTOMATED, T.quality_conditions),

    Test("T-06", "§318", "No Bad sample is ever silently valid",
         "The whole archive",
         "Count Bad samples that carry a value",
         "Zero",
         AUTOMATED, T.never_silently_valid),

    Test("T-07", "§335", "Source timestamp retention",
         "All acquired samples from the simulated unit",
         "Compare source_ts and server_ts across the archive",
         "No row with server_ts ≤ source_ts; none identical",
         AUTOMATED, T.source_timestamp_retention),

    Test("T-08", "§319, §382, §648", "Archive outage and ordered recovery",
         "Stop TimescaleDB for three minutes during acquisition",
         "collector.outage_test — ledger of acquired samples against the archive",
         "Zero loss, ascending source_ts on replay, zero duplicates",
         HOLD, None,
         note="Runs for ~9 minutes and stops the database. Executed separately; "
              "the result is in fat/records/stage-03-collector.md. A hold point: "
              "the FAT does not proceed until it has passed."),

    Test("T-09", "§455, §649", "Kill the primary collector",
         "Two collectors running; SIGKILL the primary mid start-up",
         "collector.redundancy_demo",
         "Zero missing samples across the transition; the event frame is complete",
         HOLD, None,
         note="Runs for ~7 minutes. Result in fat/records/stage-11-redundancy.md."),

    Test("T-10", "§442", "Compression ratio and reconstruction error",
         "Two-stage compression over live and synthetic series",
         "collector/test_compression.py plus the live compression report",
         "Reconstruction error ≤ CompDev; ratio reported per tag",
         AUTOMATED, T.compression),

    Test("T-11", "§472", "Start-up event auto-captured",
         "A cold start-up on the simulator",
         "Detect event frames over the archived history",
         "Frame captured automatically with all six milestones",
         AUTOMATED, T.startup_event),

    Test("T-12", "§317", "Non-intrusiveness",
         "Source scan rate and CPU, collector attached and detached",
         "Read the simulator's own scan counter and process CPU in both states",
         "Source scan rate unchanged within 5%",
         AUTOMATED, T.non_intrusiveness,
         note="Detaches the collector for 20 s. Do not run during another test."),

    Test("T-13", "§346, §336", "Time source loss",
         "A DataValue with no SourceTimestamp, and a non-advancing timestamp",
         "Inspection of the defined behaviour plus archive consistency checks",
         "Behaviour defined; no row violates it",
         AUTOMATED, T.time_source_behaviour),

    Test("T-14", "§303, §315", "No control path",
         "The whole collector package",
         "AST inspection of every source file, in code and in comments",
         "No write call and no write method; the detection rule proven able to fail",
         AUTOMATED, T.no_control_path),

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

    Test("T-18", "§509", "Role-based access",
         "Six roles with permissions as data",
         "ops/test_ops.py",
         "Each role carries only its permissions; tokens stored hashed",
         AUTOMATED, None,
         note="Covered by the automated suite; see "
              "fat/records/stage-13-remaining-requirements.md."),

    Test("T-19", "§503", "Machine-readable export",
         "A populated archive",
         "ops.export over a window, then inspect the archive produced",
         "CSV and JSON with quality on every row; Bad exports an empty value",
         AUTOMATED, None,
         note="Covered by the automated suite."),

    Test("T-20", "§433", "Audit trail on configuration change",
         "Any configuration change",
         "Inspect audit_log for actor, old value, new value and reason",
         "Every change recorded with all four",
         AUTOMATED, None,
         note="Covered by the automated suite."),
]
