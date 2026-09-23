"""Stage 11 acceptance test.

From the build plan: run two collector instances against the same source and
archive; kill the primary mid-run and assert zero missing samples across the
transition. The stated test is: kill the primary collector during a start-up
event, and the event frame is still complete.

Both collectors write freely. There is no arbitration, because the
(tag_id, source_ts) primary key makes a duplicate impossible — redundancy here
needs no consensus protocol, no fencing and no split-brain handling, and that
is a property of the schema rather than of any code.

"ZERO MISSING SAMPLES" IS CHECKED LITERALLY. The simulator keeps a ledger of
every value it published that a subscription must report (sim/ledger.py); every
one of those across the transition is looked for in the archive by its exact
source timestamp. The first version of this test looked instead for gaps in
coverage longer than each tag's heartbeat -- which would have passed with most
samples missing, as long as some arrived every minute.
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


def buffer_for(instance: str) -> Path:
    """A buffer file of the demonstration's own. The running collector's buffer
    is never touched: stopping it with SIGTERM flushes its in-flight samples
    there, and deleting that file would throw them away."""
    return ROOT / "buffer" / f"redundancy-{instance}.sqlite"


EVENT_PORTS = {"primary": "8090", "secondary": "8091"}


def start_collector(instance: str) -> subprocess.Popen:
    # Each on its own event-stream port: two processes cannot both listen on
    # 8090. (Before the collector learned to carry on without its event stream,
    # the second one to start exited on the bind failure.)
    return subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "collector",
         "--instance", instance, "--buffer", str(buffer_for(instance))],
        cwd=str(ROOT), env={**os.environ, "COLLECTOR_INSTANCE": instance,
                            "COLLECTOR_EVENT_PORT": EVENT_PORTS[instance]},
        stdout=open(f"/tmp/collector-{instance}.log", "w"),
        stderr=subprocess.STDOUT)


def leaders() -> list[tuple]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT instance, pid, alive, is_leader, samples, link_up"
                    " FROM collector_leader")
        return cur.fetchall()


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _us(when: dt.datetime) -> int:
    delta = when - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def missing_against_ledger(tags: list[str], since: dt.datetime,
                           until: dt.datetime) -> dict[str, tuple[int, list[int]]]:
    """Per tag: how many samples the source published in the window, and
    which of them (microseconds) the archive does not hold."""
    from urllib.parse import quote
    with urllib.request.urlopen(
            f"{CONTROL}/ledger?since={quote(since.isoformat())}"
            f"&until={quote(until.isoformat())}&tags={','.join(tags)}",
            timeout=30) as r:
        ledger = json.loads(r.read())["tags"]
    out = {}
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for tag in tags:
            cur.execute("SELECT s.source_ts FROM sample s JOIN tag t"
                        " ON t.id = s.tag_id WHERE t.name = %s"
                        " AND s.source_ts BETWEEN %s AND %s", (tag, since, until))
            held = {_us(r[0]) for r in cur.fetchall()}
            published = ledger.get(tag, [])
            out[tag] = (len(published), [u for u in published if u not in held])
    return out


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
    ap.add_argument("--startup-seconds", type=float, default=None,
                    help="how long the simulator's start-up takes; read from "
                         "the simulator if not given")
    args = ap.parse_args()
    checks: dict[str, bool] = {}
    processes: dict[str, subprocess.Popen] = {}
    was_running = False

    try:
        print("=" * 78)
        print("STAGE 11 — redundancy: kill the primary during a start-up")
        print("=" * 78)

        status = sim_status()
        if status is None:
            print("\n  the simulator is not running; start it first")
            return 1
        # Timed from the simulator's own start-up length. The first version took
        # a default of 180 s while the simulator ran a 90 s start-up, so the
        # "mid start-up" kill landed wherever that happened to put it.
        startup_s = args.startup_seconds or float(status["startup_seconds"])

        was_running = subprocess.run(
            ["pgrep", "-f", "Python -m collector$"], capture_output=True,
            text=True).stdout.strip() != ""
        # Stop any running collector (SIGTERM: it flushes to its own buffer and
        # drains it next time it starts), and start the demonstration's two
        # from empty buffers of their own.
        subprocess.run(["pkill", "-TERM", "-f", "Python -m collector"],
                       capture_output=True)
        time.sleep(3)
        for instance in ("primary", "secondary"):
            for path in buffer_for(instance).parent.glob(
                    buffer_for(instance).name + "*"):
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
        print(f"\n  restarting the start-up sequence ({startup_s:.0f}s)")
        urllib.request.urlopen(urllib.request.Request(
            CONTROL + "/restart", method="POST"), timeout=5).read()
        began = dt.datetime.now(dt.timezone.utc)

        # Kill the primary partway through, while the unit is still coming up.
        time.sleep(startup_s * 0.35)
        kill_instant = dt.datetime.now(dt.timezone.utc)
        phase = (sim_status() or {}).get("phase")
        print(f"\n  killing the PRIMARY at {kill_instant.strftime('%H:%M:%S')} UTC "
              f"(unit in {phase})")
        processes["primary"].send_signal(signal.SIGKILL)
        processes["primary"].wait(timeout=10)
        print("     primary is gone")

        # --- let the start-up finish on the secondary alone ---------------
        deadline = time.monotonic() + startup_s * 1.5
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
        print("EVERY SAMPLE THE SOURCE PUBLISHED, ACROSS THE TRANSITION")
        print("=" * 78)
        print("  from the simulator's own ledger, looked for in the archive by")
        print("  exact source timestamp. Heartbeat gaps are shown beside it.\n")
        tags = ["U1_MW", "U1_TURB_SPEED", "U1_MS_TEMP", "U1_MS_PRESS",
                "U1_DRUM_PRESS", "U1_COAL_FLOW", "U1_FEEDWATER_FLOW",
                "U1_AUX_POWER", "U1_CONDENSER_VAC", "U1_GEN_STATOR_TEMP",
                "U1_BEARING_VIB", "U1_BOILER_LIGHTUP", "U1_TURB_ROLLING",
                "U1_BREAKER_CLOSED"]
        # Stop short of the end: the last seconds may still be in flight.
        missing = missing_against_ledger(tags, began,
                                         ended - dt.timedelta(seconds=10))
        total_published = sum(n for n, _ in missing.values())
        total_missing = sum(len(m) for _, m in missing.values())
        for tag in tags:
            published, lost = missing[tag]
            gaps, threshold = coverage_gaps(tag, began, ended)
            print(f"  {tag:<20} published {published:>5}  missing {len(lost):>3}"
                  f"   heartbeat gaps > {threshold:4.0f}s: {len(gaps)}")
            if lost:
                first = _EPOCH + dt.timedelta(microseconds=lost[0])
                print(f"      first missing {first.isoformat()}")
        print(f"\n  published across the transition: {total_published:,}; "
              f"missing from the archive: {total_missing}")
        checks["the source published samples across the transition"] = (
            total_published > 0)
        checks["zero missing samples across the transition"] = total_missing == 0

        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT count(*), count(DISTINCT (s.tag_id, s.source_ts))
                FROM sample s JOIN tag t ON t.id = s.tag_id
                WHERE t.source_system='opcua' AND s.source_ts BETWEEN %s AND %s
            """, (began, ended))
            total, distinct = cur.fetchone()
        print(f"\n  rows in the window: {total:,}   distinct keys: {distinct:,}")
        print("  (equal by construction: the primary key forbids a second copy.")
        print("   Shown, not tested -- it is why no arbitration is needed.)")

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
        spanning = [f for f in closed if f.start_ts < kill_instant < f.end_ts]
        if closed:
            frame = spanning[-1] if spanning else closed[-1]
            print(f"  start {frame.start_ts.strftime('%H:%M:%S')}  "
                  f"end {frame.end_ts.strftime('%H:%M:%S')}  "
                  f"duration {events._hms(frame.duration_s)}  (UTC)")
            killed_shown = False
            for m in frame.milestones:
                if not killed_shown and m.ts > kill_instant:
                    print(f"    {'-- primary killed':<18} "
                          f"{events._hms((kill_instant - frame.start_ts).total_seconds()):>12}")
                    killed_shown = True
                offset = (m.ts - frame.start_ts).total_seconds()
                print(f"    {m.name:<18} {events._hms(offset):>12}")
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
        if was_running:
            # Put back what was running before: the standalone collector, which
            # drains anything it flushed to its buffer when it was stopped.
            subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "collector"],
                             cwd=str(ROOT), stdout=open("/tmp/collector.log", "a"),
                             stderr=subprocess.STDOUT, start_new_session=True)
            print("\n  the standalone collector has been restarted")


if __name__ == "__main__":
    raise SystemExit(main())
