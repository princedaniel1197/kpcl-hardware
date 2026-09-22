"""Stage 8 acceptance test.

From the build plan: run a simulated start-up. The frame is captured
automatically with all milestones. Run a second, slower one. The comparison
reports the delta per milestone.

The demo drives the simulator itself so the whole thing is one reproducible
command: a fast cold start-up, then a slower one, then detection and comparison
over the real archived history.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

from engine import events

ROOT = Path(__file__).parent.parent
DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
CONTROL = "http://127.0.0.1:8081"
ENDPOINT = "opc.tcp://127.0.0.1:4840/orianode/crpms/"


def status() -> dict | None:
    try:
        with urllib.request.urlopen(CONTROL + "/status", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def stop_simulator() -> None:
    subprocess.run(["pkill", "-f", "Python -m sim"], capture_output=True)
    subprocess.run(["pkill", "-f", "python -m sim"], capture_output=True)
    for _ in range(20):
        if status() is None:
            return
        time.sleep(0.5)


def start_simulator(startup_seconds: float) -> subprocess.Popen:
    env = {**os.environ, "SIM_STARTUP_SECONDS": str(startup_seconds)}
    process = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "sim",
         "--endpoint", ENDPOINT, "--control-port", "8081",
         "--startup-seconds", str(startup_seconds)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if status() is not None:
            return process
        time.sleep(0.5)
    raise RuntimeError("simulator did not come up")


def run_startup(startup_seconds: float, label: str) -> None:
    print(f"\n  {label}: cold start-up over {startup_seconds:.0f}s")
    stop_simulator()
    process = start_simulator(startup_seconds)
    deadline = time.monotonic() + startup_seconds * 2 + 120
    last = None
    while time.monotonic() < deadline:
        s = status()
        if s and s["phase"] != last:
            last = s["phase"]
            print(f"      {s['elapsed_s']:7.1f}s  {last}")
        if s and s["phase"] == "STEADY":
            break
        time.sleep(3)
    # Let the collector see full load settle before the frame is closed.
    time.sleep(20)


def show(frame: events.Frame, heading: str) -> None:
    print(f"\n  {heading}")
    print(f"    start    {frame.start_ts.isoformat(timespec='seconds')}")
    print(f"    end      {frame.end_ts.isoformat(timespec='seconds') if frame.end_ts else '(open)'}"
          f"   status {frame.status}"
          f"   duration {events._hms(frame.duration_s) if frame.duration_s else '—'}")
    print(f"    {'milestone':<18} {'offset':>12}  {'value':>10}  quality")
    for m in frame.milestones:
        offset = (m.ts - frame.start_ts).total_seconds()
        value = f"{m.value:10.2f}" if m.value is not None else "         —"
        print(f"    {m.name:<18} {events._hms(offset):>12}  {value}  {m.quality}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", type=float, default=120.0)
    ap.add_argument("--slow", type=float, default=240.0)
    ap.add_argument("--skip-runs", action="store_true",
                    help="detect over existing history instead of running new start-ups")
    args = ap.parse_args()
    checks: dict[str, bool] = {}
    began = dt.datetime.now(dt.timezone.utc)

    print("=" * 78)
    print("STAGE 8 — event frames: capture, milestones, comparison")
    print("=" * 78)

    if not args.skip_runs:
        run_startup(args.fast, "RUN 1 (fast)")
        run_startup(args.slow, "RUN 2 (slower)")

    templates = events.load_templates(ROOT / "config" / "event_templates.json")
    template = templates["ThermalStartup"]

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM element WHERE asset_code='KPCL-RTPS-U1'")
            element_id = cur.fetchone()[0]

        since = began - dt.timedelta(minutes=2)
        until = dt.datetime.now(dt.timezone.utc)
        detected = events.detect(conn, template, element_id, since, until)
        closed = [f for f in detected if f.status == "closed"]
        print(f"\n  detection window {since.strftime('%H:%M:%S')} .. "
              f"{until.strftime('%H:%M:%S')}")
        print(f"  detected {len(detected)} frame(s), {len(closed)} closed")
        for f in detected:
            print(f"      {f.status:<8} {f.start_ts.strftime('%H:%M:%S')} -> "
                  f"{f.end_ts.strftime('%H:%M:%S') if f.end_ts else '(open)'}"
                  f"   {len(f.milestones)} milestones")
        checks["two start-ups were captured"] = len(closed) >= 2

        if len(closed) < 2:
            print("\n  FAIL: fewer than two completed start-ups in the window")
            return 1

        ids = [events.store(conn, f) for f in closed]
        first, second = closed[-2], closed[-1]
        first_id, second_id = ids[-2], ids[-1]

        show(first, "FRAME 1 (fast run)")
        show(second, "FRAME 2 (slower run)")

        expected = {m.name for m in template.milestones}
        checks["frame 1 has every milestone"] = {
            m.name for m in first.milestones} == expected
        checks["frame 2 has every milestone"] = {
            m.name for m in second.milestones} == expected
        checks["milestones are in order in frame 1"] = all(
            a.ts <= b.ts for a, b in zip(first.milestones, first.milestones[1:]))
        checks["the second run really was slower"] = (
            second.duration_s > first.duration_s)

        # Mark the fast one as the reference curve (§475).
        with conn.cursor() as cur:
            cur.execute("UPDATE event_frame SET is_reference = false"
                        " WHERE element_id=%s AND template=%s",
                        (element_id, template.name))
            cur.execute("UPDATE event_frame SET is_reference = true,"
                        " notes = 'reference curve' WHERE id = %s", (first_id,))
            conn.commit()

        reference = events.reference_frame(conn, template.name, element_id)
        checks["a reference curve is stored"] = reference is not None

        print("\n" + "=" * 78)
        print("COMPARISON (§475) — frame 2 against the reference curve")
        print("=" * 78)
        deltas = events.compare(second, reference)
        for d in deltas:
            print(f"    {d.describe()}")
        checks["comparison reports a delta for every milestone"] = all(
            d.delta_s is not None for d in deltas)
        checks["the slower run is reported as later"] = any(
            (d.delta_s or 0) > 0 for d in deltas)

        best = events.best_frame(conn, template.name, element_id,
                                 exclude_id=second_id)
        print(f"\n  against the previous best "
              f"({events._hms(best.duration_s)}):")
        for d in events.compare(second, best):
            print(f"    {d.describe()}")
        checks["previous best is identified"] = (
            best is not None and best.duration_s <= second.duration_s)

        # The query the build plan asks for.
        listing = events.frames_for_element(conn, element_id, template.name)
        print(f"\n  frames_for_element: {len(listing)} frame(s) with milestone tables")
        checks["query returns frames with milestones"] = bool(
            listing and listing[0]["milestones"])

        print("\n" + "=" * 78)
        for name, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed = [n for n, ok in checks.items() if not ok]
        print("=" * 78)
        print(f"STAGE 8: {'PASS' if not failed else 'FAIL — ' + ', '.join(failed)}")
        return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
