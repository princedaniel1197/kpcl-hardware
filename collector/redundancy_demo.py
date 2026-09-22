"""Stage 11 acceptance test.

From the build plan: run two collector instances against the same source and
archive; kill the primary mid-run and assert zero missing samples across the
transition. The stated test is: kill the primary collector during a start-up
event, and the event frame is still complete.

Both collectors write freely. There is no arbitration, because the
(tag_id, source_ts) primary key makes a duplicate impossible — redundancy here
needs no consensus protocol, no fencing and no split-brain handling, and that
is a property of the schema rather than of any code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

import psycopg

from engine import events

ROOT = Path(__file__).parent.parent
DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
CONTROL = "http://127.0.0.1:8081"


def sim_status() -> dict | None:
    try:
        with urllib.request.urlopen(CONTROL + "/status", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def start_collector(instance: str) -> subprocess.Popen:
    return subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "collector",
         "--instance", instance],
        cwd=str(ROOT), env={**os.environ, "COLLECTOR_INSTANCE": instance},
        stdout=open(f"/tmp/collector-{instance}.log", "w"),
        stderr=subprocess.STDOUT)


def leaders() -> list[tuple]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT instance, pid, alive, is_leader, samples, link_up"
                    " FROM collector_leader")
        return cur.fetchall()


def coverage_gaps(tag: str, since: dt.datetime, until: dt.datetime,
                  tolerance_s: float = 5.0) -> tuple[list[tuple], float]:
    """Gaps in archive coverage larger than the tag's own max-time heartbeat.

    The threshold has to be per tag, not a fixed number. A tag reports on
    change, so one that is genuinely not moving — turbine speed at standstill,
    generation before synchronisation — produces samples only at its max_time
    interval, and that is correct behaviour rather than a loss. Measured against
    a flat 6 s threshold, U1_TURB_SPEED showed a "62.5 s gap" that was exactly
    its 60 s heartbeat.

    Measured on the archive rather than on either collector's account of itself,
    because what matters is what survived, not what each process believed.
    """
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT max_time_ms FROM tag WHERE name = %s", (tag,))
        row = cur.fetchone()
        threshold_s = (row[0] or 60000) / 1000.0 + tolerance_s
        cur.execute("""
            SELECT prev_ts, source_ts, EXTRACT(epoch FROM source_ts - prev_ts)
            FROM (
              SELECT s.source_ts,
                     lag(s.source_ts) OVER (ORDER BY s.source_ts) AS prev_ts
              FROM sample s JOIN tag t ON t.id = s.tag_id
              WHERE t.name = %s AND s.source_ts BETWEEN %s AND %s
            ) g
            WHERE prev_ts IS NOT NULL
              AND EXTRACT(epoch FROM source_ts - prev_ts) > %s
            ORDER BY 3 DESC
        """, (tag, since, until, threshold_s))
        return cur.fetchall(), threshold_s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--startup-seconds", type=float, default=180.0)
    args = ap.parse_args()
    checks: dict[str, bool] = {}
    processes: dict[str, subprocess.Popen] = {}

    try:
        print("=" * 78)
        print("STAGE 11 — redundancy: kill the primary during a start-up")
        print("=" * 78)

        if sim_status() is None:
            print("\n  the simulator is not running; start it first")
            return 1

        subprocess.run(["pkill", "-f", "Python -m collector"],
                       capture_output=True)
        time.sleep(2)
        for path in ROOT.glob("buffer/*.sqlite*"):
            path.unlink()

        print("\n  starting two collectors against the same source and archive")
        processes["primary"] = start_collector("primary")
        time.sleep(3)
        processes["secondary"] = start_collector("secondary")
        time.sleep(12)

        print(f"\n  {'instance':<12} {'pid':>7}  alive  leader   samples  link")
        for instance, pid, alive, is_leader, samples, link in leaders():
            print(f"  {instance:<12} {pid:>7}  {str(alive):<5}  "
                  f"{str(is_leader):<7} {samples:>7}  {link}")
        rows = {r[0]: r for r in leaders()}
        checks["both instances registered"] = len(rows) >= 2
        checks["exactly one leader"] = sum(
            1 for r in rows.values() if r[3]) == 1
        checks["the leader is the primary"] = rows.get("primary", (None,)*4)[3]

        # --- restart the start-up and kill the primary mid-way ------------
        print(f"\n  restarting the start-up sequence ({args.startup_seconds:.0f}s)")
        urllib.request.urlopen(urllib.request.Request(
            CONTROL + "/restart", method="POST"), timeout=5).read()
        began = dt.datetime.now(dt.timezone.utc)

        # Kill the primary partway through, while the unit is still coming up.
        time.sleep(args.startup_seconds * 0.35)
        kill_instant = dt.datetime.now(dt.timezone.utc)
        phase = (sim_status() or {}).get("phase")
        print(f"\n  killing the PRIMARY at {kill_instant.strftime('%H:%M:%S')} "
              f"(unit in {phase})")
        processes["primary"].send_signal(signal.SIGKILL)
        processes["primary"].wait(timeout=10)
        print("     primary is gone")

        # --- let the start-up finish on the secondary alone ---------------
        deadline = time.monotonic() + args.startup_seconds * 1.5
        while time.monotonic() < deadline:
            if (sim_status() or {}).get("phase") == "STEADY":
                break
            time.sleep(5)
        time.sleep(25)
        ended = dt.datetime.now(dt.timezone.utc)

        print(f"\n  {'instance':<12} {'pid':>7}  alive  leader   samples")
        for instance, pid, alive, is_leader, samples, _ in leaders():
            print(f"  {instance:<12} {pid:>7}  {str(alive):<5}  "
                  f"{str(is_leader):<7} {samples:>7}")
        after = {r[0]: r for r in leaders()}
        checks["leadership moved to the survivor"] = (
            after.get("secondary", (None,)*4)[3] is True)

        # --- zero missing samples across the transition -------------------
        print("\n" + "=" * 78)
        print("COVERAGE ACROSS THE TRANSITION")
        print("=" * 78)
        print("  threshold is each tag's own max-time heartbeat plus 5 s: a tag")
        print("  that is not moving reports only at that interval, by design.\n")
        for tag in ("U1_MS_TEMP", "U1_TURB_SPEED", "U1_MW", "U1_DRUM_PRESS"):
            gaps, threshold = coverage_gaps(tag, began, ended)
            worst = f"{gaps[0][2]:.1f}s" if gaps else "none"
            print(f"  {tag:<16} threshold {threshold:5.1f}s   gaps: "
                  f"{len(gaps):<3} worst: {worst}")
            checks[f"no coverage gap on {tag}"] = not gaps

        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT count(*), count(DISTINCT (s.tag_id, s.source_ts))
                FROM sample s JOIN tag t ON t.id = s.tag_id
                WHERE t.source_system='opcua' AND s.source_ts BETWEEN %s AND %s
            """, (began, ended))
            total, distinct = cur.fetchone()
        print(f"\n  rows in the window: {total:,}   distinct keys: {distinct:,}")
        checks["zero duplicates despite two writers"] = total == distinct

        # --- the event frame is still complete ----------------------------
        print("\n" + "=" * 78)
        print("THE EVENT FRAME")
        print("=" * 78)
        templates = events.load_templates(ROOT / "config" / "event_templates.json")
        template = templates["ThermalStartup"]
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM element WHERE asset_code='KPCL-RTPS-U1'")
                element_id = cur.fetchone()[0]
            frames = events.detect(conn, template, element_id,
                                   began - dt.timedelta(seconds=30), ended)
            closed = [f for f in frames if f.status == "closed"]
            for f in closed:
                events.store(conn, f)

        expected = {m.name for m in template.milestones}
        if closed:
            frame = closed[-1]
            print(f"  start {frame.start_ts.strftime('%H:%M:%S')}  "
                  f"end {frame.end_ts.strftime('%H:%M:%S')}  "
                  f"duration {events._hms(frame.duration_s)}")
            for m in frame.milestones:
                offset = (m.ts - frame.start_ts).total_seconds()
                marker = "  <-- primary killed here" if (
                    m.ts > kill_instant and
                    (m.ts - kill_instant).total_seconds() < 60) else ""
                print(f"    {m.name:<18} {events._hms(offset):>12}{marker}")
            checks["the start-up frame was captured"] = True
            checks["every milestone is present"] = {
                m.name for m in frame.milestones} == expected
            checks["the frame spans the kill"] = (
                frame.start_ts < kill_instant < frame.end_ts)
        else:
            print("  no completed frame detected")
            checks["the start-up frame was captured"] = False

        print("\n" + "=" * 78)
        for name, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed = [n for n, ok in checks.items() if not ok]
        print("=" * 78)
        print(f"STAGE 11: {'PASS' if not failed else 'FAIL — ' + ', '.join(failed)}")
        return 0 if not failed else 1
    finally:
        for p in processes.values():
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
