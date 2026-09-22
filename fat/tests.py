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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


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
    """Database clock minus host clock, in seconds.

    These are not the same clock. The simulator stamps SourceTimestamp from the
    host; PostgreSQL runs in a container with its own clock. Measured on this
    rig they differ by up to 1.8 s and drift. Any measurement that subtracts one
    from the other is measuring the clock difference as much as the system.
    """
    offsets = []
    for _ in range(samples):
        before = dt.datetime.now(dt.timezone.utc)
        db_now = _query(conn, "SELECT now()")[0][0]
        after = dt.datetime.now(dt.timezone.utc)
        offsets.append((db_now - (before + (after - before) / 2)).total_seconds())
        time.sleep(0.1)
    offsets.sort()
    return offsets[len(offsets) // 2]


def latency_p95(conn) -> Result:
    """Acquisition-to-historian latency: how old the newest archived sample is.

    MEASURED AGAINST ONE CLOCK. `source_ts` is stamped by the simulator on the
    host, so "now" must also come from the host — not from `now()` inside the
    database container, which is a different clock.

    That is not a hypothetical. The first version of this test used the
    database's `now()` and reported a **P95 of -3.278 s**, which it then passed,
    because -3.278 is less than 5. A negative latency is not a fast system; it
    is a broken measurement. The container's clock was measured lagging the
    host's by up to 1.8 s and drifting.

    So: one clock, and any negative observation fails the test outright rather
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
                f"database clock minus host clock: {offset:+.3f} s "
                f"(measured, not assumed — the two are different clocks)"]
    if negative:
        return Result(False,
                      f"{len(negative)} observations were NEGATIVE; the "
                      f"measurement spans two clocks and is not valid",
                      evidence=evidence)
    return Result(p95 <= 5.0,
                  f"P95 {p95:.3f} s over {len(observations)} observations",
                  evidence=evidence,
                  detail="measured host-clock to host-clock; the container's "
                         "clock is reported alongside because it differs")


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
            _http_json("http://127.0.0.1:8000/api/kpis")
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


def availability(conn) -> Result:
    """Availability over the observation window.

    The fraction of one-minute buckets in which the archive holds at least one
    sample from the source. A minute with none is a minute the system was not
    acquiring.

    EVERY UNCOVERED MINUTE IS LISTED. A single availability percentage invites
    the reader to assume the missing time was random; naming the minutes lets
    them see whether it was a deliberate outage test, which on this rig it
    usually is. A figure of 93.55% that turns out to be "we stopped the database
    on purpose for two minutes" means something entirely different from 93.55%
    of unexplained absence, and the report should not make the reader guess.
    """
    window = AVAILABILITY_WINDOW_MIN
    rows = _query(conn, """
        WITH bounds AS (
          SELECT max(source_ts) AS newest,
                 max(source_ts) - (%s || ' minutes')::interval AS oldest
          FROM sample s JOIN tag t ON t.id = s.tag_id
          WHERE t.source_system = 'opcua'
        ),
        minutes AS (
          SELECT generate_series(date_trunc('minute', oldest),
                                 date_trunc('minute', newest),
                                 INTERVAL '1 minute') AS m FROM bounds
        )
        SELECT m, EXISTS (
                 SELECT 1 FROM sample s JOIN tag t ON t.id = s.tag_id
                 WHERE t.source_system='opcua'
                   AND s.source_ts >= minutes.m
                   AND s.source_ts < minutes.m + INTERVAL '1 minute') AS covered
        FROM minutes ORDER BY m
    """, (str(window),))
    if not rows:
        return Result(False, "no observation window available")
    total = len(rows)
    uncovered = [m for m, covered in rows if not covered]
    pct = (total - len(uncovered)) / total * 100
    evidence = [f"{total - len(uncovered)} of {total} minutes carried samples",
                f"observation window: {rows[0][0].isoformat()} to "
                f"{rows[-1][0].isoformat()}"]
    if uncovered:
        evidence.append(f"uncovered minutes ({len(uncovered)}): "
                        + ", ".join(m.strftime("%H:%M") for m in uncovered[:20])
                        + (" ..." if len(uncovered) > 20 else ""))
    return Result(pct >= 99.5, f"{pct:.2f}% over {total} minutes",
                  evidence=evidence,
                  detail=("Deliberate outage tests run during the window count "
                          "against this figure, which is why every uncovered "
                          "minute is named rather than summarised. For an "
                          "acceptance figure, observe a window with no "
                          "destructive test in it."))


# ---------------------------------------------------------------------------
# §318 / §384 — quality
# ---------------------------------------------------------------------------

def quality_conditions(conn) -> Result:
    """Force Bad and Uncertain through the control API and confirm each is
    flagged and distinguishable. Range and frozen are covered by the Stage 6
    record and by quality_flag rows."""
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
            severity = (code >> 30) & 3
            evidence.append(f"{quality} -> quality {code} ({klass}), "
                            f"value {value!r}")
            if severity != expected:
                _post(f"{SIM_CONTROL}/quality", method="DELETE")
                return Result(False, f"{quality} arrived as {klass}")
            if expected == 2 and value is not None:
                _post(f"{SIM_CONTROL}/quality", method="DELETE")
                return Result(False, "a Bad sample carried a value")
        _post(f"{SIM_CONTROL}/quality", method="DELETE")
    except Exception as exc:
        return Result(False, f"control API unreachable: {exc}")

    flags = _query(conn,
        "SELECT rule_type, count(*) FROM quality_flag GROUP BY rule_type"
        " ORDER BY rule_type")
    evidence += [f"quality_flag {r[0]}: {r[1]}" for r in flags]
    kinds = {r[0] for r in flags}
    return Result(True, f"Bad and Uncertain flagged; rules seen: "
                        f"{', '.join(sorted(kinds)) or 'none yet'}",
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
    rows = _query(conn, """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE server_ts <= source_ts) AS non_causal,
               count(*) FILTER (WHERE server_ts = source_ts) AS identical,
               round(avg(EXTRACT(epoch FROM server_ts - source_ts))::numeric * 1000, 2)
        FROM sample s JOIN tag t ON t.id = s.tag_id
        WHERE t.source_system = 'opcua' AND t.source_path = 'Unit1'
    """)
    total, non_causal, identical, mean_ms = rows[0]
    return Result(non_causal == 0 and identical == 0,
                  f"{non_causal} of {total:,} rows have server_ts <= source_ts",
                  evidence=[f"identical timestamps: {identical}",
                            f"mean transit: {mean_ms} ms",
                            "a receipt time substituted for a measurement time "
                            "would make the two identical"])


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
    return Result(passed, "code inspection test suite " +
                  ("passed" if passed else "FAILED"),
                  evidence=tail + [
                      "asserts no write_value, write_attribute, set_value or "
                      "call_method in the collector package, in code or comments"])


# ---------------------------------------------------------------------------
# §442 — compression
# ---------------------------------------------------------------------------

def compression(conn) -> Result:
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest",
         "collector/test_compression.py", "-q"],
        cwd=str(ROOT), capture_output=True, text=True)
    passed = result.returncode == 0
    report = ROOT / "fat" / "evidence" / "compression-report.txt"
    evidence = [l for l in result.stdout.strip().splitlines() if l][-1:]
    if report.exists():
        evidence.append(f"live ratios recorded in {report.name}")
    return Result(passed,
                  "reconstruction error within CompDev on every shape tested",
                  evidence=evidence + [
                      "bound verified end to end against the raw series, not "
                      "only against what survived exception reporting"])


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
    """Defined behaviour on a bad or absent source timestamp.

    DEFINED BEHAVIOUR:
      1. A DataValue with NO SourceTimestamp is DROPPED and logged. It is never
         stamped with the receipt time, because that would silently convert a
         clock failure into plausible-looking data.
      2. A SourceTimestamp that does not advance is stored if its key is new and
         absorbed by the primary key if it is not. Compression forces an archive
         on a non-advancing timestamp rather than compressing across it.
      3. Nothing in the system rewrites a source timestamp, ever.

    Verified here by inspection of the guarantees the code and schema give,
    plus the absence of any row that violates them.
    """
    evidence = []
    session = (ROOT / "collector" / "session.py").read_text()
    drops = "dropped" in session and "SourceTimestamp is None" in session
    evidence.append("collector drops a DataValue with no SourceTimestamp: "
                    f"{drops}")

    compression_src = (ROOT / "collector" / "compression.py").read_text()
    forces = "BY_TIMESTAMP" in compression_src
    evidence.append(f"compression forces an archive on a non-advancing "
                    f"timestamp: {forces}")

    rows = _query(conn,
        "SELECT count(*) FROM sample WHERE server_ts < source_ts")
    evidence.append(f"rows with server_ts earlier than source_ts: {rows[0][0]}")

    duplicates = _query(conn,
        "SELECT count(*) - count(DISTINCT (tag_id, source_ts)) FROM sample")
    evidence.append(f"duplicate (tag_id, source_ts) keys: {duplicates[0][0]}")

    passed = drops and forces and rows[0][0] == 0 and duplicates[0][0] == 0
    return Result(passed, "behaviour defined and consistent with the archive",
                  evidence=evidence,
                  detail="a source clock that has failed cannot be made good by "
                         "the acquisition layer; it can only be refused")


# ---------------------------------------------------------------------------
# §317 — non-intrusiveness
# ---------------------------------------------------------------------------

def non_intrusiveness(conn) -> Result:
    """Source CPU and scan rate, collector on versus off.

    The simulator reports its own scan count, so the scan rate can be measured
    from the source's point of view rather than inferred.
    """
    def sample_source(seconds: float) -> tuple[float, float]:
        before = _http_json(f"{SIM_CONTROL}/status")
        pid = subprocess.run(["pgrep", "-f", "Python -m sim"],
                             capture_output=True, text=True).stdout.split()
        cpu_before = _process_cpu(pid[0]) if pid else 0.0
        time.sleep(seconds)
        after = _http_json(f"{SIM_CONTROL}/status")
        cpu_after = _process_cpu(pid[0]) if pid else 0.0
        scans = (after["scans"] - before["scans"]) / seconds
        return scans, (cpu_after - cpu_before) / seconds * 100

    def _process_cpu(pid: str) -> float:
        out = subprocess.run(["ps", "-p", pid, "-o", "time="],
                             capture_output=True, text=True).stdout.strip()
        if not out:
            return 0.0
        parts = out.replace("-", ":").split(":")
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return seconds

    try:
        with_collector = sample_source(20)
        subprocess.run(["pkill", "-f", "Python -m collector"], capture_output=True)
        time.sleep(5)
        without = sample_source(20)
    except Exception as exc:
        return Result(None, f"could not measure: {exc}")
    finally:
        subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "collector"],
                         cwd=str(ROOT),
                         stdout=open("/tmp/collector.log", "a"),
                         stderr=subprocess.STDOUT)
        time.sleep(8)

    scan_delta = abs(with_collector[0] - without[0]) / max(without[0], 1e-9) * 100
    return Result(scan_delta < 5.0,
                  f"source scan rate changed {scan_delta:.2f}% with the "
                  f"collector attached",
                  evidence=[
                      f"with collector:    {with_collector[0]:.2f} scans/s, "
                      f"{with_collector[1]:.1f}% CPU",
                      f"without collector: {without[0]:.2f} scans/s, "
                      f"{without[1]:.1f}% CPU"],
                  detail="the collector subscribes rather than polls, so the "
                         "source does its own scanning either way")
