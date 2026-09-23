"""The FAT test definitions and the checks that can be automated.

Each test carries the clause it exists for, the numeric acceptance criterion,
and how it is verified. A test that cannot be automated says so and becomes a
hold or witness point in the procedure rather than quietly disappearing.

Nothing here computes a result from an assumption. Every number in a report is
measured at the time the report is produced.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).parent.parent
DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
SIM_CONTROL = os.environ.get("SIM_CONTROL", "http://127.0.0.1:8081")
COLLECTOR_HEALTH = os.environ.get("COLLECTOR_HEALTH", "http://127.0.0.1:8090/health")

AUTOMATED = "automated"
WITNESS = "witness"          # must be observed by a person
HOLD = "hold"                # work stops until signed off


@dataclass
class Result:
    passed: bool | None          # None = not run
    actual: str
    evidence: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class Test:
    ref: str
    clause: str
    title: str
    condition: str
    method: str
    criterion: str
    kind: str = AUTOMATED
    run: object = None           # callable(conn) -> Result
    note: str = ""
    # What change to the system would make this test fail. A test for which
    # there is no such change passes because of how it was written, not
    # because of what the system does -- and an evaluator who finds one such
    # test will rightly assume the rest are the same.
    fails_if: str = ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# Bearer token for the API (§509). The runner creates a principal for the run,
# puts its token here, and revokes it afterwards.
API_TOKEN: str | None = None
API_URL = os.environ.get("CRPMS_API_URL", "http://127.0.0.1:8000")


def _http_json(url: str, timeout: float = 5.0, token: str | None = None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as r:
        return json.loads(r.read())


def _pytest(*args: str, env: dict | None = None) -> tuple[bool, list[str]]:
    """Run part of the automated suite; return pass/fail and its summary line."""
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *args], cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ, **(env or {})})
    lines = [l for l in result.stdout.strip().splitlines() if l]
    return result.returncode == 0, lines[-1:]


def _post(url: str, body: dict | None = None, method: str = "POST"):
    data = json.dumps(body).encode() if body else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=5) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# §528 — performance
# ---------------------------------------------------------------------------

def clock_offset(conn, samples: int = 7) -> float:
    """Database clock minus host clock, in seconds, median of `samples`.

    The simulator stamps SourceTimestamp from the host; PostgreSQL runs in a
    container. They are measured, not assumed, to agree -- on this rig to
    within about a millisecond. (An earlier version of this function read
    `now()` and reported offsets of up to 1.8 s "and drifting". That was not
    the container's clock: it was the age of the runner's own transaction.)
    """
    offsets = []
    for _ in range(samples):
        before = dt.datetime.now(dt.timezone.utc)
        # clock_timestamp(), not now(): now() is the start of the current
        # transaction, and on a connection that has been open for the whole
        # run it is minutes stale -- which is how an earlier report printed
        # an "offset" that was really the age of its own transaction.
        db_now = _query(conn, "SELECT clock_timestamp()")[0][0]
        after = dt.datetime.now(dt.timezone.utc)
        offsets.append((db_now - (before + (after - before) / 2)).total_seconds())
        time.sleep(0.1)
    offsets.sort()
    return offsets[len(offsets) // 2]


def latency_p95(conn) -> Result:
    """Acquisition-to-historian latency: how old the newest archived sample is.

    MEASURED AGAINST ONE CLOCK. `source_ts` is stamped by the simulator on the
    host, so "now" is taken from the host too.

    The first version of this test used the database's `now()` and reported a
    **P95 of -3.278 s**, which it then passed, because -3.278 is less than 5.
    A negative latency is not a fast system; it is a broken measurement. The
    cause was first written down as the container's clock lagging the host's.
    That was wrong, and was found on 23 September: `now()` in PostgreSQL is the
    start time of the current TRANSACTION, the runner held one connection with
    one transaction open for the whole run, and so "now" was minutes in the
    past. The container's clock agrees with the host's to about a millisecond.

    So: one clock, the offset between the two measured with clock_timestamp()
    and reported, and any negative observation fails the test outright rather
    than being averaged away.
    """
    offset = clock_offset(conn)
    observations: list[float] = []
    # Each observation is a real query against the live archive, so 1000 of them
    # takes a couple of minutes. The criterion says 1000; the test waits for
    # 1000 rather than reporting a percentile over fewer and calling it the same
    # thing.
    deadline = time.monotonic() + 240
    while len(observations) < 1000 and time.monotonic() < deadline:
        rows = _query(conn,
            "SELECT max(s.source_ts) FROM sample s JOIN tag t ON t.id = s.tag_id"
            " WHERE t.source_system='opcua' AND t.name LIKE %s",
            ("U1\\_%",))
        if rows and rows[0][0] is not None:
            # Host clock on both sides of the subtraction.
            observations.append(
                (dt.datetime.now(dt.timezone.utc) - rows[0][0]).total_seconds())
        time.sleep(0.05)

    if len(observations) < 1000:
        return Result(False, f"only {len(observations)} observations",
                      detail="fewer than the 1000 the criterion requires")

    negative = [o for o in observations if o < 0]
    observations.sort()
    p95 = observations[int(len(observations) * 0.95)]
    p50 = statistics.median(observations)
    evidence = [f"P50 {p50:.3f} s", f"P95 {p95:.3f} s",
                f"max {observations[-1]:.3f} s", f"min {observations[0]:.3f} s",
                f"database clock minus host clock: {offset * 1000:+.1f} ms "
                f"(clock_timestamp(), median of 7)"]
    if negative:
        return Result(False,
                      f"{len(negative)} observations were NEGATIVE; the "
                      f"measurement is not valid",
                      evidence=evidence)
    return Result(p95 <= 5.0,
                  f"P95 {p95:.3f} s over {len(observations)} observations",
                  evidence=evidence,
                  detail="measured host clock to host clock; the database "
                         "clock's offset is reported alongside")


def trend_query_24h(conn) -> Result:
    rows = _query(conn,
        "SELECT t.id, t.name FROM tag t JOIN sample s ON s.tag_id = t.id"
        " GROUP BY t.id, t.name ORDER BY count(*) DESC LIMIT 1")
    if not rows:
        return Result(False, "no samples in the archive")
    tag_id, tag_name = rows[0]
    span = _query(conn, "SELECT min(source_ts), max(source_ts) FROM sample"
                        " WHERE tag_id = %s", (tag_id,))[0]
    start = span[0]
    timings = []
    for _ in range(5):
        began = time.monotonic()
        _query(conn,
            "SELECT source_ts, value, quality FROM sample WHERE tag_id = %s"
            " AND source_ts >= %s AND source_ts < %s + INTERVAL '24 hours'"
            " ORDER BY source_ts", (tag_id, start, start))
        timings.append(time.monotonic() - began)
    returned = len(_query(conn,
        "SELECT 1 FROM sample WHERE tag_id = %s AND source_ts >= %s"
        " AND source_ts < %s + INTERVAL '24 hours'", (tag_id, start, start)))
    worst = max(timings)
    return Result(worst <= 5.0 and returned > 0,
                  f"{worst * 1000:.0f} ms worst of 5, {returned:,} rows",
                  evidence=[f"tag {tag_name}",
                            f"median {statistics.median(timings) * 1000:.0f} ms",
                            f"rows returned {returned:,}"])


def dashboard_refresh(conn) -> Result:
    """The UI polls the API on a fixed interval; the criterion is 2-3 s."""
    source = (ROOT / "ui" / "src" / "App.jsx").read_text()
    import re
    matches = [int(m) for m in re.findall(r"setInterval\(poll, (\d+)\)", source)]
    if not matches:
        return Result(False, "no dashboard poll interval found in App.jsx")
    interval = matches[0] / 1000
    # And measure that the API actually answers inside that budget.
    timings = []
    for _ in range(10):
        began = time.monotonic()
        try:
            _http_json(f"{API_URL}/api/kpis", token=API_TOKEN)
            timings.append(time.monotonic() - began)
        except Exception as exc:
            return Result(False, f"API unreachable: {exc}")
    worst = max(timings)
    return Result(2.0 <= interval <= 3.0 and worst < interval,
                  f"{interval:.1f} s interval, API answers in "
                  f"{worst * 1000:.0f} ms worst of 10",
                  evidence=[f"configured interval {interval:.1f} s",
                            f"API p50 {statistics.median(timings) * 1000:.0f} ms"])


AVAILABILITY_WINDOW_MIN = float(os.environ.get("FAT_AVAILABILITY_MINUTES", "60"))
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _us(when: dt.datetime) -> int:
    delta = when - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def availability(conn) -> Result:
    """Availability over the observation window, measured against the source.

    The first version counted one-minute buckets holding at least one sample.
    With fourteen tags at 2 Hz that is one sample out of about 1,680 a minute:
    it would have reported 100 % while losing almost everything. So now:

    SAMPLE COMPLETENESS. The simulator keeps its own ledger of every value it
    published that an OPC UA subscription is obliged to report (sim/ledger.py).
    Every one of those, per tag, is looked for in the archive by its exact
    source timestamp. Completeness is found / published.

    BUCKET COVERAGE is still reported, beside it, because a minute with nothing
    in it is worth naming even when completeness is high.

    Every uncovered minute and every missing sample's minute is listed, so a
    deliberate outage test in the window can be told from unexplained loss.
    """
    status = _http_json(f"{SIM_CONTROL}/status")
    if not status.get("ledger_since"):
        return Result(False, "the simulator has no ledger yet")
    ledger_since = dt.datetime.fromisoformat(status["ledger_since"])
    # Leave the most recent seconds out: they may still be in flight, which is
    # latency (T-01), not loss.
    until = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=15)
    since = max(until - dt.timedelta(minutes=AVAILABILITY_WINDOW_MIN),
                ledger_since + dt.timedelta(seconds=5))
    names = [r[0] for r in _query(conn,
        "SELECT name FROM tag WHERE source_system='opcua' AND source_path='Unit1'"
        " ORDER BY name")]
    ledger = _http_json(
        f"{SIM_CONTROL}/ledger?since={urllib.parse.quote(since.isoformat())}"
        f"&until={urllib.parse.quote(until.isoformat())}&tags={','.join(names)}",
        timeout=30)["tags"]
    archived: dict[str, set[int]] = {n: set() for n in names}
    for name, source_ts in _query(conn,
            "SELECT t.name, s.source_ts FROM sample s JOIN tag t ON t.id = s.tag_id"
            " WHERE t.name = ANY(%s) AND s.source_ts BETWEEN %s AND %s",
            (names, since, until)):
        archived[name].add(_us(source_ts))

    expected = found = 0
    missing_minutes: dict[str, int] = {}
    for name, stamps in ledger.items():
        for us in stamps:
            expected += 1
            if us in archived.get(name, ()):
                found += 1
            else:
                minute = (_EPOCH + dt.timedelta(microseconds=us)).strftime("%H:%M")
                missing_minutes[minute] = missing_minutes.get(minute, 0) + 1
    if not expected:
        return Result(False, "the source published nothing in the window")
    completeness = found / expected * 100

    minutes = [since.replace(second=0, microsecond=0) + dt.timedelta(minutes=i)
               for i in range(int((until - since).total_seconds() // 60) + 1)]
    covered = {m for m, in _query(conn,
        "SELECT DISTINCT date_trunc('minute', s.source_ts) FROM sample s"
        " JOIN tag t ON t.id = s.tag_id WHERE t.name = ANY(%s)"
        " AND s.source_ts BETWEEN %s AND %s", (names, since, until))}
    uncovered = [m for m in minutes if m not in covered]
    coverage = (len(minutes) - len(uncovered)) / len(minutes) * 100

    evidence = [
        f"window {since.isoformat(timespec='seconds')} to "
        f"{until.isoformat(timespec='seconds')} "
        f"({(until - since).total_seconds() / 60:.1f} min), {len(names)} Unit 1 tags",
        f"published by the source (its own ledger): {expected:,}",
        f"found in the archive by exact source timestamp: {found:,}",
        f"missing: {expected - found:,}",
        f"one-minute buckets with data: {len(minutes) - len(uncovered)} of "
        f"{len(minutes)} ({coverage:.2f}%)",
    ]
    if missing_minutes:
        evidence.append("minutes with missing samples: " + ", ".join(
            f"{m} ({n})" for m, n in sorted(missing_minutes.items())[:20]))
    if uncovered:
        evidence.append("uncovered minutes: " + ", ".join(
            m.strftime("%H:%M") for m in uncovered[:20]))
    return Result(completeness >= 99.5 and coverage >= 99.5,
                  f"{completeness:.3f}% of {expected:,} published samples archived; "
                  f"{coverage:.2f}% of minutes covered",
                  evidence=evidence,
                  detail=("Anything that stops acquisition in the window -- a "
                          "deliberate outage test, a collector restart, T-12's "
                          "detach -- counts against these figures, which is why "
                          "the minutes are named. For an acceptance figure, "
                          "observe a window with no destructive test in it."))


# ---------------------------------------------------------------------------
# §318 / §384 — quality
# ---------------------------------------------------------------------------

def quality_conditions(conn) -> Result:
    """Force each of the four conditions and confirm each is flagged, with its
    own StatusCode, distinguishable from source quality.

    Two parts. First, Bad and Uncertain are forced at the SOURCE through the
    simulator's control API and read back from the archive: each must arrive
    with its own StatusCode, and a Bad sample must carry no value. Second, the
    Stage 6 demonstration is run as it stands (engine/quality_demo.py): it
    forces out-of-range, cross-tag, rate-of-change and frozen on the live
    system and checks the verdict each rule actually produced.

    The configuration changes the demonstration makes to force a condition --
    narrowing an EURange, tightening a tolerance -- are audited and restored.
    Earlier runs made them without an audit row, and left 1,251 range flags on
    U1_MS_TEMP that looked like a defect in the tag's configuration with
    nothing to say otherwise; this run's flags are counted separately from
    history, and history is attributed to the audit rows that explain it.
    """
    began = dt.datetime.now(dt.timezone.utc)
    evidence = []
    try:
        _post(f"{SIM_CONTROL}/quality", method="DELETE")
        time.sleep(3)
        for quality, expected in (("BadDeviceFailure", 2),
                                  ("UncertainSensorNotAccurate", 1)):
            _post(f"{SIM_CONTROL}/quality/U1_MS_PRESS", {"quality": quality})
            time.sleep(4)
            rows = _query(conn,
                "SELECT s.value, s.quality, quality_class(s.quality)"
                " FROM sample s JOIN tag t ON t.id = s.tag_id"
                " WHERE t.name='U1_MS_PRESS' ORDER BY s.source_ts DESC LIMIT 1")
            if not rows:
                return Result(False, "no samples for U1_MS_PRESS")
            value, code, klass = rows[0]
            evidence.append(f"forced {quality} at the source -> archived "
                            f"{code} ({klass}), value {value!r}")
            if (code >> 30) & 3 != expected:
                return Result(False, f"{quality} arrived as {klass}", evidence)
            if expected == 2 and value is not None:
                return Result(False, "a Bad sample carried a value", evidence)
    except Exception as exc:
        return Result(False, f"control API unreachable: {exc}")
    finally:
        try:
            _post(f"{SIM_CONTROL}/quality", method="DELETE")
        except Exception:
            pass

    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "engine.quality_demo"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=420)
    lines = result.stdout.splitlines()
    checks = [l.strip() for l in lines if l.strip().startswith(("PASS ", "FAIL "))]
    verdict = next((l for l in lines if l.startswith("STAGE 6:")), "no verdict")
    evidence += [f"quality_demo: {c}" for c in checks]

    flagged = _query(conn,
        "SELECT rule_type, count(*) FROM quality_flag WHERE detected_at >= %s"
        " GROUP BY rule_type ORDER BY rule_type", (began,))
    evidence.append("flags raised by this run: " + (", ".join(
        f"{r} {n}" for r, n in flagged) or "none"))
    explained = _query(conn,
        "SELECT count(*) FROM audit_log WHERE actor = 'stage6-demo' AND ts >= %s",
        (began,))[0][0]
    evidence.append(f"audited configuration changes made to force them: {explained}")
    passed = result.returncode == 0 and verdict.endswith("PASS")
    return Result(passed, f"Bad and Uncertain arrive with their own codes; {verdict}",
                  evidence=evidence)


def never_silently_valid(conn) -> Result:
    """No Bad sample anywhere in the archive carries a value."""
    rows = _query(conn,
        "SELECT count(*) FROM sample WHERE ((quality >> 30) & 3) = 2"
        " AND value IS NOT NULL")
    offenders = rows[0][0]
    total_bad = _query(conn,
        "SELECT count(*) FROM sample WHERE ((quality >> 30) & 3) = 2")[0][0]
    return Result(offenders == 0,
                  f"{offenders} of {total_bad:,} Bad samples carry a value",
                  evidence=[f"Bad samples in the archive: {total_bad:,}"])


# ---------------------------------------------------------------------------
# §335 — source timestamp retention
# ---------------------------------------------------------------------------

def source_timestamp_retention(conn) -> Result:
    """The source timestamp is kept; a receipt time never replaces it.

    If the collector stamped samples with the time it received them, the
    stored source_ts would be LATER than the server's publish time, and
    server_ts <= source_ts would appear; if it copied one timestamp into the
    other, the two would be identical. Either fails this test.

    A sample no server stamped has server_ts NULL (migration 014) and cannot be
    compared; it is counted separately rather than counted as a pass. Against
    this simulator the count is expected to be zero -- asyncua always stamps.
    """
    rows = _query(conn, """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE server_ts IS NULL) AS unstamped,
               count(*) FILTER (WHERE server_ts <= source_ts) AS non_causal,
               count(*) FILTER (WHERE server_ts = source_ts) AS identical,
               round(avg(EXTRACT(epoch FROM server_ts - source_ts))::numeric * 1000, 2)
        FROM sample s JOIN tag t ON t.id = s.tag_id
        WHERE t.source_system = 'opcua' AND t.source_path = 'Unit1'
    """)
    total, unstamped, non_causal, identical, mean_ms = rows[0]
    return Result(total > 0 and non_causal == 0 and identical == 0,
                  f"{non_causal} of {total - unstamped:,} comparable rows have "
                  f"server_ts <= source_ts",
                  evidence=[f"identical timestamps: {identical}",
                            f"rows with no server timestamp (not compared): "
                            f"{unstamped}",
                            f"mean transit: {mean_ms} ms"])


# ---------------------------------------------------------------------------
# §303 — no control path
# ---------------------------------------------------------------------------

def no_control_path(conn) -> Result:
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest",
         "collector/test_readonly.py", "-q"],
        cwd=str(ROOT), capture_output=True, text=True)
    passed = result.returncode == 0
    tail = [l for l in result.stdout.strip().splitlines() if l][-1:]
    from collector.test_readonly import FORBIDDEN_CALLS, source_files
    return Result(passed, "code inspection test suite " +
                  ("passed" if passed else "FAILED"),
                  evidence=tail + [
                      f"{len(source_files())} source files inspected, recursively",
                      f"{len(FORBIDDEN_CALLS)} forbidden calls, including writes, "
                      "method calls and node management (add_*/delete_*), in code "
                      "or in comments",
                      "a planted write_value() is caught by the same rule"])


# ---------------------------------------------------------------------------
# §442 — compression
# ---------------------------------------------------------------------------

COMPRESSION_SECONDS = float(os.environ.get("FAT_COMPRESSION_SECONDS", "90"))


def compression(conn) -> Result:
    """Two parts, both required.

    The library bound: collector/test_compression.py, including the textbook
    swinging door shown to EXCEED CompDev on the same series, so the test can
    fail. And the live path: collector/compression_report.py subscribes to the
    simulator and runs every sample through the collector's own Pipeline with
    compression on, then checks the reconstruction of what would be archived
    against everything the source produced.

    What this does NOT show, and the report says so: in the running system
    compression is per tag and off by default, so the archive this FAT reads is
    uncompressed. That keeps the zero-loss measurement of T-04 and T-08 a
    statement about every sample, not about samples within CompDev.
    """
    unit_ok, unit_tail = _pytest("collector/test_compression.py",
                                 "collector/test_pipeline.py")
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "collector.compression_report",
         "--seconds", str(COMPRESSION_SECONDS)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    report = ROOT / "fat" / "evidence" / "compression-report.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(result.stdout)
    lines = result.stdout.splitlines()
    summary = [" ".join(l.split()).replace(" :", ":") for l in lines
               if l.strip().startswith(("tags within", "too few", "overall",
                                        "best ratio", "lowest ratio"))]
    ms_temp = next((l.split() for l in lines if l.startswith("U1_MS_TEMP")), None)
    ratio_ok = ms_temp is not None and float(ms_temp[3].split(":")[0]) > 10.0
    compressed = _query(conn, "SELECT count(*) FROM tag WHERE compress")[0][0]
    evidence = unit_tail + summary + [
        f"U1_MS_TEMP live ratio: {ms_temp[3] if ms_temp else 'not measured'} "
        f"(build plan: > 10:1)",
        f"full table in fat/evidence/{report.name}",
        f"tags archived compressed in the running system: {compressed}"]
    passed = unit_ok and result.returncode == 0 and ratio_ok
    return Result(passed,
                  "error within CompDev in the library and in the live path; "
                  + (summary[0] if summary else "no live result"),
                  evidence=evidence)


# ---------------------------------------------------------------------------
# §472 — event frames
# ---------------------------------------------------------------------------

def startup_event(conn) -> Result:
    rows = _query(conn, """
        SELECT f.id, f.start_ts, f.end_ts, count(m.id)
        FROM event_frame f LEFT JOIN event_milestone m ON m.event_frame_id = f.id
        WHERE f.template = 'ThermalStartup' AND f.status = 'closed'
        GROUP BY f.id, f.start_ts, f.end_ts ORDER BY f.start_ts DESC LIMIT 5
    """)
    if not rows:
        return Result(False, "no completed start-up frames captured")
    complete = [r for r in rows if r[3] >= 6]
    newest = rows[0]
    duration = (newest[2] - newest[1]).total_seconds() if newest[2] else None
    return Result(bool(complete),
                  f"{len(complete)} of {len(rows)} recent frames have all six "
                  f"milestones",
                  evidence=[f"newest frame {newest[1].isoformat()} "
                            f"lasted {duration:.0f}s with {newest[3]} milestones"])


# ---------------------------------------------------------------------------
# §346, §336 — time source loss
# ---------------------------------------------------------------------------

def time_source_behaviour(conn) -> Result:
    """Defined behaviour on a bad or absent source timestamp, exercised.

    DEFINED BEHAVIOUR:
      1. A DataValue with NO SourceTimestamp is refused and counted
         (DROP_NO_SOURCE_TS). It is never stamped with the receipt time: that
         would silently convert a clock failure into plausible-looking data.
      2. A DataValue with no ServerTimestamp is kept, with server_ts NULL --
         never filled from the source timestamp.
      3. A source timestamp that does not advance -- the same instant
         delivered twice -- is absorbed and counted (DUPLICATE_TS) before it
         can take a sequence number. One that goes backwards is a new key and
         is stored; compression, where enabled, forces an archive on it
         rather than compressing across it.
      4. Nothing in the system rewrites a source timestamp, ever.

    1 to 3 are exercised here against the collector's own code, not inferred
    from its source text; the archive is then checked for any row that breaks
    them.
    """
    import datetime as _dt
    from types import SimpleNamespace

    from asyncua import ua as _ua

    from collector.buffer import Buffer
    from collector.config import CollectorConfig
    from collector.events import EventStream
    from collector.pipeline import Pipeline
    from collector.session import IngressCounts, _SubscriptionHandler

    import logging
    # The refusal below is logged at ERROR by design; here it is expected.
    logging.getLogger("collector.session").setLevel(logging.CRITICAL)
    evidence = []
    got, counts = [], IngressCounts()
    handler = _SubscriptionHandler(got.append, {"n": {"id": 1, "name": "T"}}, counts)
    t0 = _dt.datetime(2030, 1, 1, tzinfo=_dt.timezone.utc)

    def dv(source, server):
        return SimpleNamespace(Value=SimpleNamespace(Value=1.0),
                               StatusCode=_ua.StatusCode(0),
                               SourceTimestamp=source, ServerTimestamp=server)

    handler.handle("n", dv(None, t0))
    refused = not got and counts.dropped_no_source_ts == 1
    evidence.append(f"no SourceTimestamp: refused and counted: {refused}")

    handler.handle("n", dv(t0, None))
    kept_null = (len(got) == 1 and got[0].server_ts is None
                 and got[0].source_ts == t0)
    evidence.append(f"no ServerTimestamp: kept with server_ts NULL: {kept_null}")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(CollectorConfig(), None, Buffer(Path(tmp) / "b.sqlite"),
                            EventStream(), run_id=1)
        pipeline.on_sample(got[0])
        pipeline.on_sample(got[0])
        queued = pipeline._take_queued()
    absorbed = len(queued) == 1 and pipeline.duplicate_ts == 1
    evidence.append(f"repeated SourceTimestamp: absorbed and counted, one "
                    f"sequence number: {absorbed}")

    rows = _query(conn,
        "SELECT count(*) FROM sample WHERE server_ts < source_ts")
    evidence.append(f"archive rows with server_ts earlier than source_ts: {rows[0][0]}")
    evidence.append("compression on a repeated timestamp: the compressor forces "
                    "it through and the archive keeps the first row for that key "
                    "(collector/compression.py, point 2)")

    passed = refused and kept_null and absorbed and rows[0][0] == 0
    return Result(passed, "behaviour exercised and consistent with the archive",
                  evidence=evidence,
                  detail="a source clock that has failed cannot be made good by "
                         "the acquisition layer; it can only be refused")


# ---------------------------------------------------------------------------
# §317 — non-intrusiveness
# ---------------------------------------------------------------------------

NI_WINDOW_S = float(os.environ.get("FAT_NI_WINDOW_SECONDS", "45"))
NI_PAIRS = int(os.environ.get("FAT_NI_PAIRS", "2"))
# Our budget, stated as ours: the build plan says "unchanged" and gives no
# number, and serving a subscription cannot cost literally nothing.
NI_CPU_PER_TAG_PCT = 0.5          # % of one core, per subscribed tag
NI_SCAN_P95_TOLERANCE = 0.10      # scan work P95 may rise by 10 % ...
NI_SCAN_P95_FLOOR_MS = 1.0        # ... or 1 ms, whichever is larger


def _pids(pattern: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                         text=True).stdout.split()
    return [int(p) for p in out]


def _source_window(sim_pid: int, seconds: float) -> dict:
    """CPU and scan timing of the source over one window, from the source's
    side: its process CPU time (microsecond resolution) and its own record of
    how long each scan's work took and how late each scan started."""
    from fat.proc_cpu import cpu_seconds
    before = _http_json(f"{SIM_CONTROL}/status")["scans"]
    cpu0, wall0 = cpu_seconds(sim_pid), time.monotonic()
    time.sleep(seconds)
    cpu1, wall1 = cpu_seconds(sim_pid), time.monotonic()
    after = _http_json(f"{SIM_CONTROL}/status")["scans"]
    scans = _http_json(f"{SIM_CONTROL}/scans?after={before}&up_to={after}")
    return {"cpu_pct": (cpu1 - cpu0) / (wall1 - wall0) * 100,
            "work_p95": scans["work_ms"]["p95"],
            "late_p95": scans["lateness_ms"]["p95"], "scans": scans["scans"]}


def non_intrusiveness(conn) -> Result:
    """Load on the source, collector attached and detached. (§317)

    The first version compared the simulator's scan RATE with the collector on
    and off. The simulator scans on a fixed schedule, so its rate cannot
    change whatever a client does: that criterion could not fail. And the CPU
    figure it reported came from `ps`, at one-second resolution.

    Now, alternating windows with the collector detached and attached:

      * source CPU, from the kernel's per-process accounting at microsecond
        resolution (fat/proc_cpu.py); the difference, on minus off, divided by
        the number of subscribed tags, is the CPU the collector costs the
        source per tag;
      * the source's scan WORK time -- computing and writing one scan, the
        thing a DCS reports as controller loading -- P95, on against off.

    Criterion: at most 0.5 % of one core per subscribed tag, and scan work P95
    no more than 10 % (or 1 ms) above detached. Both numbers are ours; the
    build plan says "unchanged", which a server answering a client cannot
    literally be. What would fail it is recorded in the plan: polling instead
    of subscribing, or subscribing far faster than configured.
    """
    sims = _pids("Python -m sim ")
    if not sims:
        return Result(None, "the simulator is not running")
    sim_pid = sims[0]
    try:
        health = _http_json(COLLECTOR_HEALTH)
        subscribed = health["source"]["subscribed"]
    except Exception as exc:
        return Result(None, f"the collector is not running: {exc}")

    windows = {"on": [], "off": []}
    try:
        for _ in range(NI_PAIRS):
            windows["on"].append(_source_window(sim_pid, NI_WINDOW_S))
            subprocess.run(["pkill", "-TERM", "-f", "Python -m collector$"],
                           capture_output=True)
            for _ in range(30):
                if not _pids("Python -m collector$"):
                    break
                time.sleep(0.5)
            time.sleep(3)
            windows["off"].append(_source_window(sim_pid, NI_WINDOW_S))
            _start_collector()
    except Exception as exc:
        return Result(None, f"could not measure: {exc}")
    finally:
        if not _pids("Python -m collector$"):
            _start_collector()

    def mean(key, state):
        return statistics.mean(w[key] for w in windows[state])

    cpu_on, cpu_off = mean("cpu_pct", "on"), mean("cpu_pct", "off")
    per_tag = (cpu_on - cpu_off) / max(subscribed, 1)
    work_on, work_off = mean("work_p95", "on"), mean("work_p95", "off")
    allowed = max(work_off * (1 + NI_SCAN_P95_TOLERANCE),
                  work_off + NI_SCAN_P95_FLOOR_MS)
    evidence = [
        f"{NI_PAIRS} pairs of {NI_WINDOW_S:.0f} s windows, {subscribed} tags "
        f"subscribed",
        "source CPU attached:  " + ", ".join(f"{w['cpu_pct']:.2f}%" for w in windows["on"]),
        "source CPU detached:  " + ", ".join(f"{w['cpu_pct']:.2f}%" for w in windows["off"]),
        f"attributable to the collector: {cpu_on - cpu_off:+.2f}% of one core, "
        f"{per_tag:+.3f}% per subscribed tag (budget {NI_CPU_PER_TAG_PCT}%)",
        f"scan work P95: attached {work_on:.3f} ms, detached {work_off:.3f} ms "
        f"(allowed {allowed:.3f} ms)",
        f"scan start lateness P95: attached {mean('late_p95', 'on'):.3f} ms, "
        f"detached {mean('late_p95', 'off'):.3f} ms",
        f"CPU measured with {__import__('fat.proc_cpu').proc_cpu.resolution_s * 1e9:.1f} ns "
        f"resolution",
    ]
    passed = per_tag <= NI_CPU_PER_TAG_PCT and work_on <= allowed
    return Result(passed,
                  f"{per_tag:.3f}% of one core per subscribed tag; scan work "
                  f"P95 {work_on:.2f} ms attached vs {work_off:.2f} ms detached",
                  evidence=evidence,
                  detail="detaching the collector is itself a gap in acquisition; "
                         "T-04 run afterwards over the same window will count it")


def _start_collector() -> None:
    log = open("/tmp/collector.log", "a")
    subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "collector"],
                     cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True)
    for _ in range(40):
        try:
            if _http_json(COLLECTOR_HEALTH, timeout=2)["source"]["subscribed"]:
                return
        except Exception:
            pass
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# §509, §503, §433 — run from the automated suite, against the live system
# ---------------------------------------------------------------------------

def role_based_access(conn) -> Result:
    """The API itself, not the access library: every route with no token, an
    unknown token, an admin token, a station token on its own station and on
    another, and the WebSocket -- against the API that is running now."""
    ok, tail = _pytest("api/test_auth.py", env={"CRPMS_API_URL": API_URL})
    lib_ok, lib_tail = _pytest("ops/test_ops.py", "-k", "role or token or station")
    return Result(ok and lib_ok,
                  f"live API: {tail[0] if tail else 'no result'}",
                  evidence=[f"api/test_auth.py against {API_URL}: "
                            f"{tail[0] if tail else '-'}",
                            f"ops/test_ops.py (roles, tokens, scope): "
                            f"{lib_tail[0] if lib_tail else '-'}",
                            "no token -> 401 on every route; station token on "
                            "another station -> 403; unmapped data refused to a "
                            "station principal; undeclared route fails closed"])


def machine_readable_export(conn) -> Result:
    ok, tail = _pytest("ops/test_ops.py", "-k", "export")
    return Result(ok, f"export tests: {tail[0] if tail else 'no result'}",
                  evidence=tail + ["CSV and JSON with the StatusCode on every row; "
                                   "a Bad sample exports an empty value, never 0"])


def audit_trail(conn) -> Result:
    ok, tail = _pytest("archive/test_audit.py", "archive/test_schema.py",
                       "ops/test_ops.py", "engine/test_assets.py",
                       "-k", "audit")
    unexplained = _query(conn,
        "SELECT count(*) FROM audit_log WHERE actor IS NULL OR reason IS NULL"
        " OR ts IS NULL")[0][0]
    return Result(ok and unexplained == 0,
                  f"audit tests: {tail[0] if tail else 'no result'}",
                  evidence=tail + [
                      f"audit rows missing actor, reason or timestamp: {unexplained}",
                      "the log is append-only: UPDATE, DELETE and TRUNCATE are "
                      "refused by the database (migration 015)",
                      "a value set to itself writes no row; numbers compare as "
                      "numbers"])
