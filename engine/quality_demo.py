"""Stage 6 acceptance test: force each condition on the live system.

From the build plan: force each of the four conditions. Each is flagged,
distinguishable from source quality, and visible downstream.

Each condition is induced the way it would actually arise, against the running
simulator and collector, on real acquired data:

  arrived Bad     forced through the simulator's control API -- the instrument
                  disowning its own reading
  out of range    a mis-scaled transmitter: the tag's EURange is narrowed, so
                  genuine readings fall outside what it claims to span
  cross-tag       the consistency tolerance is tightened past the real
                  agreement between feedwater flow and generation
  rate of change  a real step: the start-up sequence is restarted, so the unit
                  drops from full load to cold in one scan
  frozen          U1_MW sits at exactly zero while the breaker is open

ORDER MATTERS and is deliberate. Restarting the start-up puts a large step into
every tag's history, so it runs after the checks that need a quiet window, and
the checks that follow it use a lookback short enough to exclude it.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

import psycopg

from archive import audit
from engine import quality as q

ACTOR = "stage6-demo"

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
CONTROL = os.environ.get("SIM_CONTROL", "http://127.0.0.1:8081")
CLASS = ("Good", "Uncertain", "Bad", "Reserved")


def control(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body else None
    request = urllib.request.Request(
        CONTROL + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def _breaker_close_fraction() -> float:
    """How far into the start-up the simulator closes the breaker: from its own
    phase table, so the two cannot disagree."""
    from sim.plant import BREAKER_CLOSE_AT, PHASE_FRACTION, PHASE_ORDER, Phase
    before = sum(PHASE_FRACTION[p] for p in PHASE_ORDER[:PHASE_ORDER.index(
        Phase.SYNCHRONISATION)])
    return before + PHASE_FRACTION[Phase.SYNCHRONISATION] * BREAKER_CLOSE_AT


def tag_id(conn, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tag WHERE name = %s", (name,))
        return cur.fetchone()[0]


def check(conn, name: str, heading: str, lookback: float = 300.0):
    flags, health = q.evaluate_tag(conn, tag_id(conn, name), lookback_s=lookback)
    q.persist(conn, flags, health)
    print(f"\n  {heading}")
    print(f"    tag              : {name}")
    print(f"    SOURCE quality   : {health.source_quality} "
          f"({CLASS[q.severity(health.source_quality or 0)]})")
    print(f"    COMPUTED quality : {health.computed_quality} "
          f"({CLASS[q.severity(health.computed_quality or 0)]})")
    print(f"    health state     : {health.worst_state}")
    seen = {}
    for f in flags:
        seen.setdefault(f.rule_type, f)
    for rule, f in seen.items():
        print(f"    flagged[{rule}]{' ' * max(0, 4 - len(rule))} : {f.reason}")
    if not flags:
        print("    flagged          : nothing")
    return health, seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", type=float, default=10.0)
    args = ap.parse_args()
    r: dict[str, bool] = {}

    with psycopg.connect(DSN) as conn:
        print("=" * 76)
        print("STAGE 6 — each condition forced on the live system")
        print("=" * 76)

        control("DELETE", "/quality")
        time.sleep(args.settle)
        base, _ = check(conn, "U1_MS_TEMP", "BASELINE — healthy tag at load")
        r["baseline is clean"] = base.worst_state == "ok"

        # --- arrived Bad: the control case --------------------------------
        control("POST", "/quality/U1_COAL_FLOW", {"quality": "BadDeviceFailure"})
        time.sleep(args.settle)
        bad, _ = check(conn, "U1_COAL_FLOW",
                       "A. ARRIVED BAD — forced through the control API")
        r["arrived Bad: health says bad"] = bad.is_bad
        r["arrived Bad: source is BadDeviceFailure"] = (
            bad.source_quality == 2156593152)
        r["arrived Bad: not reported as out of range"] = not bad.is_out_of_range
        control("DELETE", "/quality/U1_COAL_FLOW")

        # --- 1. out of range ----------------------------------------------
        # A configuration change like any other, so it is audited like any
        # other, and restored whatever happens. The flags this leaves in
        # quality_flag are then explained by the audit log rather than looking
        # like a defect in the tag configuration.
        tid = tag_id(conn, "U1_MS_TEMP")
        with conn.cursor() as cur:
            cur.execute("SELECT range_high FROM tag WHERE id=%s", (tid,))
            original_range = cur.fetchone()[0]
            audit.change(cur, "tag", tid, "range_high", 300.0, actor=ACTOR,
                         reason="Stage 6 test: EURange narrowed to force an "
                                "out-of-range condition; restored after")
            conn.commit()
        try:
            oor, rules = check(conn, "U1_MS_TEMP",
                               "1. OUT OF RANGE — EURange narrowed to 0-300 degC")
        finally:
            with conn.cursor() as cur:
                audit.change(cur, "tag", tid, "range_high", original_range,
                             actor=ACTOR, reason="Stage 6 test: EURange restored")
                conn.commit()
        r["out of range flagged"] = "range" in rules
        r["out of range: source stayed Good"] = q.severity(oor.source_quality or 0) == 0
        r["out of range: computed is Bad"] = (
            "range" in rules and rules["range"].computed_quality == q.OUT_OF_RANGE)

        # --- 2. cross-tag, while the unit is still at load ------------------
        fw = tag_id(conn, "U1_FEEDWATER_FLOW")
        with conn.cursor() as cur:
            cur.execute("SELECT id, params FROM quality_rule WHERE tag_id=%s"
                        " AND rule_type='cross_tag'", (fw,))
            rule_id, original_params = cur.fetchone()
            audit.change(cur, "quality_rule", rule_id, "params",
                         {**original_params, "tolerance": 0.0001}, actor=ACTOR,
                         reason="Stage 6 test: cross-tag tolerance tightened to "
                                "force an inconsistency; restored after")
            conn.commit()
        try:
            cross, rules = check(conn, "U1_FEEDWATER_FLOW",
                                 "2. CROSS-TAG — tolerance tightened to 0.01%",
                                 lookback=30.0)
        finally:
            with conn.cursor() as cur:
                audit.change(cur, "quality_rule", rule_id, "params",
                             original_params, actor=ACTOR,
                             reason="Stage 6 test: tolerance restored")
                conn.commit()
        r["cross-tag flagged"] = "cross_tag" in rules
        # The verdict the rule actually produced, not the constant it uses.
        r["cross-tag: Uncertain not Bad"] = (
            "cross_tag" in rules
            and q.severity(rules["cross_tag"].computed_quality) == 1)

        # --- 3. rate of change: a real step ---------------------------------
        print("\n  3. RATE OF CHANGE — restarting the start-up sequence")
        print("     (full load to cold in one scan; this puts a step into")
        print("      every tag, which is why it runs after the checks above)")
        control("POST", "/restart")
        time.sleep(args.settle)
        rate, rules = check(conn, "U1_MS_TEMP", "     after the step",
                            lookback=60.0)
        r["rate of change flagged"] = "rate_of_change" in rules
        r["rate of change: Uncertain not Bad"] = (
            "rate_of_change" in rules
            and q.severity(rules["rate_of_change"].computed_quality) == 1)
        r["rate of change: source stayed Good"] = (
            q.severity(rate.source_quality or 0) == 0)

        # --- 4. frozen -------------------------------------------------------
        # U1_MW is exactly 0 while the breaker is open, and the check has to
        # land in that stretch: after the frozen window and a heartbeat have
        # passed since the restart, and before the breaker closes. The first
        # version slept a fixed 65 s. With a 90 s start-up the breaker closes
        # 58 s in, so by then U1_MW was ramping and "frozen" could not be
        # flagged; it passed on 22 September only because the timings then
        # happened to fit. Now the timing is taken from the simulator's own
        # clock and its own start-up length, and a start-up too short to hold
        # the window says so instead of reporting a failure of the rule.
        tid_mw = tag_id(conn, "U1_MW")
        with conn.cursor() as cur:
            cur.execute("SELECT params FROM quality_rule WHERE tag_id=%s"
                        " AND rule_type='frozen'", (tid_mw,))
            window_s = float(cur.fetchone()[0]["window_seconds"])
            cur.execute("SELECT max_time_ms FROM tag WHERE id=%s", (tid_mw,))
            heartbeat_s = (cur.fetchone()[0] or 60000) / 1000.0
        status = control("GET", "/status")
        breaker_closes_s = status["startup_seconds"] * _breaker_close_fraction()
        ready_s = window_s + 2 * heartbeat_s + 2.0
        print("\n  4. FROZEN — U1_MW is exactly 0 until the breaker closes")
        print(f"     ({breaker_closes_s:.0f} s into the start-up). Checking once")
        print(f"     {ready_s:.0f} s of it has passed: the {window_s:.0f} s frozen window")
        print(f"     plus two {heartbeat_s:.0f} s max-time reads, which are where the")
        print("     samples of an unchanging tag come from.")
        if ready_s >= breaker_closes_s - 1.0:
            print("     the start-up is too short to hold the frozen window with the")
            print("     breaker open; run the simulator with a longer start-up")
            r["frozen: start-up long enough to test"] = False
            rules, frozen = {}, None
        else:
            while control("GET", "/status")["elapsed_s"] < ready_s:
                time.sleep(0.5)
            frozen, rules = check(conn, "U1_MW",
                                  "     U1_MW pinned at 0, breaker open",
                                  lookback=ready_s + 30.0)
        r["frozen flagged"] = "frozen" in rules
        r["frozen: source stayed Good"] = (
            frozen is not None and q.severity(frozen.source_quality or 0) == 0)

        # --- the distinction --------------------------------------------------
        print("\n" + "=" * 76)
        print("THE DISTINCTION (§318, CLAUDE.md rule 2)")
        print("=" * 76)
        print(f"  arrived Good, failed range : source={oor.source_quality:<12} "
              f"computed={oor.computed_quality}")
        print(f"  arrived Bad                : source={bad.source_quality:<12} "
              f"computed={bad.computed_quality}")
        distinct = (oor.source_quality != bad.source_quality
                    and oor.computed_quality != bad.source_quality
                    and oor.is_out_of_range and bad.is_bad
                    and not oor.is_bad and not bad.is_out_of_range)
        r["the two are distinguishable"] = distinct
        print(f"\n  a value that arrived Good and failed a check, and a value")
        print(f"  the instrument disowned, are different facts : {distinct}")

        print("\n" + "=" * 76)
        for name, ok in r.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed = [n for n, ok in r.items() if not ok]
        print("=" * 76)
        print(f"STAGE 6: {'PASS' if not failed else 'FAIL — ' + ', '.join(failed)}")
        return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
