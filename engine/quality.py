"""Data quality rules (§384) and tag health (§439).

THE DISTINCTION THIS MODULE EXISTS TO PRESERVE. A value that arrived Good but
failed a range check is not the same thing as a value that arrived Bad. The
first is a measurement the instrument stands behind and the system doubts; the
second is one the instrument itself disowns. They call for different responses,
and a system that cannot tell them apart is guessing.

So source quality is never overwritten. `sample.quality` holds the StatusCode
exactly as acquired, for ever. Everything computed here is stored alongside it
in `quality_flag`, and both appear in the `sample_quality` view.

The four rules of §384:
  1. range check against the tag's EURange
  2. rate-of-change check
  3. cross-tag consistency
  4. frozen or stale value detection

Thresholds are per tag, held in `quality_rule.params`, because retuning a limit
must be a row change rather than an edit here (§341).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import psycopg
from asyncua import ua

# Computed quality is a real OPC UA StatusCode, like every other quality in this
# project. The choices below are deliberate rather than convenient:
#
#   out of range -> Bad. The reading is outside what the instrument can
#     represent, so it is not a measurement at all.
#   rate of change, cross-tag, frozen -> Uncertain. The reading may be perfectly
#     real; what is in doubt is whether to believe it. Calling these Bad would
#     discard data that is probably fine, and this project does not throw away
#     values it merely distrusts.
#   stale -> Bad, because nothing is arriving; there is no measurement to judge.
OUT_OF_RANGE = int(ua.StatusCodes.BadOutOfRange)
# Plain Uncertain: OPC UA has no code for "changing faster than is plausible".
# It used to be UncertainSensorNotAccurate, which says the value is at a sensor
# limit -- a specific claim this rule does not make. The reason text carries
# the detail.
RATE_EXCEEDED = int(ua.StatusCodes.Uncertain)
INCONSISTENT = int(ua.StatusCodes.UncertainSubNormal)
FROZEN = int(ua.StatusCodes.UncertainLastUsableValue)
STALE = int(ua.StatusCodes.BadNoCommunication)
GOOD = int(ua.StatusCodes.Good)

RULES = ("range", "rate_of_change", "cross_tag", "frozen", "stale")


def severity(code: int) -> int:
    """OPC UA severity from the top two bits: 0 Good, 1 Uncertain, 2 Bad."""
    return (code >> 30) & 3


@dataclass(frozen=True)
class Flag:
    tag_id: int
    tag_name: str
    source_ts: dt.datetime
    rule_type: str
    computed_quality: int
    reason: str


@dataclass(frozen=True)
class Health:
    """Current state of one tag (§439)."""
    tag_id: int
    tag_name: str
    evaluated_at: dt.datetime
    last_source_ts: dt.datetime | None
    is_bad: bool = False
    is_stale: bool = False
    is_frozen: bool = False
    is_missing: bool = False
    is_out_of_range: bool = False
    is_comm_failed: bool = False
    source_quality: int | None = None
    computed_quality: int | None = None
    detail: str | None = None

    @property
    def worst_state(self) -> str:
        for flag, name in ((self.is_missing, "missing"),
                           (self.is_comm_failed, "comm_failed"),
                           (self.is_bad, "bad"), (self.is_stale, "stale"),
                           (self.is_frozen, "frozen"),
                           (self.is_out_of_range, "out_of_range")):
            if flag:
                return name
        return "ok"


# -- the four rules ----------------------------------------------------------

def check_range(value: float | None, low: float | None,
                high: float | None) -> tuple[int, str] | None:
    """Rule 1: against the tag's EURange (§436)."""
    if value is None or (low is None and high is None):
        return None
    if low is not None and value < low:
        return OUT_OF_RANGE, f"value {value:g} below EURange low {low:g}"
    if high is not None and value > high:
        return OUT_OF_RANGE, f"value {value:g} above EURange high {high:g}"
    return None


def check_rate_of_change(value: float | None, previous: float | None,
                         seconds: float, max_per_second: float
                         ) -> tuple[int, str] | None:
    """Rule 2. A step larger than the process can physically produce says the
    instrument jumped, not that the plant did."""
    if value is None or previous is None or seconds <= 0:
        return None
    rate = abs(value - previous) / seconds
    if rate > max_per_second:
        return (RATE_EXCEEDED,
                f"rate {rate:.4g}/s exceeds limit {max_per_second:g}/s "
                f"(moved {value - previous:+.4g} in {seconds:g}s)")
    return None


def check_cross_tag(value: float | None, other: float | None, *,
                    other_name: str, ratio: float, tolerance: float
                    ) -> tuple[int, str] | None:
    """Rule 3: consistency between two tags that describe the same physics.

    The pair configured in this project is feedwater flow against main steam
    flow: in steady operation what goes into the boiler as water comes out as
    steam, so a sustained divergence means one of the two instruments is wrong
    (or there is a genuine leak, which is also worth knowing).
    """
    if value is None or other is None:
        return None
    expected = other * ratio
    if expected == 0:
        return None
    deviation = abs(value - expected) / abs(expected)
    if deviation > tolerance:
        return (INCONSISTENT,
                f"{value:g} differs from {other_name} x {ratio:g} = "
                f"{expected:g} by {deviation * 100:.1f}%, tolerance "
                f"{tolerance * 100:.1f}%")
    return None


def check_frozen(samples: list[tuple[dt.datetime, float | None]],
                 max_seconds: float, tolerance: float = 0.0
                 ) -> tuple[int, str] | None:
    """Rule 4a: arriving, but not moving.

    A frozen tag is the dangerous failure, because it looks healthy. The samples
    are Good, they are on time, and the number never changes -- which is exactly
    what a stuck transmitter produces and also what a genuinely steady process
    produces, so the threshold is per tag.

    Takes timestamped samples rather than a bare list and a duration. The
    earlier signature filtered to the last `window` seconds and then required
    that slice to SPAN `window`, which it can never do -- the rule could not
    fire at all. It also could not tell "unchanged for five minutes" from "we
    only have twenty seconds of history", and those are different claims.
    """
    if len(samples) < 2:
        return None
    last_ts = samples[-1][0]
    cutoff = last_ts - dt.timedelta(seconds=max_seconds)

    # The history must actually reach back across the window. Without that we
    # cannot say the value has been unchanged for that long, only that it has
    # not changed in what we happen to hold.
    if samples[0][0] > cutoff:
        return None

    window = [(ts, v) for ts, v in samples if ts >= cutoff and v is not None]
    present = [v for _, v in window]
    if len(present) < 2:
        return None
    spread = max(present) - min(present)
    if spread <= tolerance:
        span = (window[-1][0] - window[0][0]).total_seconds()
        return (FROZEN,
                f"value unchanged within {tolerance:g} for {span:.0f}s "
                f"(limit {max_seconds:g}s, {len(present)} samples, "
                f"spread {spread:g})")
    return None


def check_stale(last_source_ts: dt.datetime | None, now: dt.datetime,
                max_seconds: float) -> tuple[int, str] | None:
    """Rule 4b: nothing arriving at all."""
    if last_source_ts is None:
        return None
    age = (now - last_source_ts).total_seconds()
    if age > max_seconds:
        return (STALE, f"no sample for {age:.0f}s (limit {max_seconds:g}s)")
    return None


# -- evaluation against the archive ------------------------------------------

DEFAULTS = {
    "range": {},
    "rate_of_change": {"max_per_second": None},
    "cross_tag": {"other_tag": None, "ratio": 1.0, "tolerance": 0.1,
                  "gate_tag": None, "gate_min": None},
    "frozen": {"window_seconds": 120.0, "tolerance": 0.0},
    "stale": {"max_seconds": 60.0},
}


def load_rules(conn: psycopg.Connection, tag_id: int | None = None
               ) -> dict[int, dict[str, dict]]:
    with conn.cursor() as cur:
        if tag_id is None:
            cur.execute("SELECT tag_id, rule_type, params FROM quality_rule"
                        " WHERE enabled")
        else:
            cur.execute("SELECT tag_id, rule_type, params FROM quality_rule"
                        " WHERE enabled AND tag_id = %s", (tag_id,))
        out: dict[int, dict[str, dict]] = {}
        for tid, rule_type, params in cur.fetchall():
            out.setdefault(tid, {})[rule_type] = params
    return out


def evaluate_tag(conn: psycopg.Connection, tag_id: int, *,
                 now: dt.datetime | None = None,
                 lookback_s: float = 300.0) -> tuple[list[Flag], Health]:
    """Run every enabled rule for one tag over its recent history."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rules = load_rules(conn, tag_id).get(tag_id, {})

    with conn.cursor() as cur:
        cur.execute("SELECT name, range_low, range_high, quality_note FROM tag"
                    " WHERE id = %s", (tag_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"no such tag: {tag_id}")
        tag_name, range_low, range_high, quality_note = row

        cur.execute(
            "SELECT source_ts, value, quality FROM sample"
            " WHERE tag_id = %s AND source_ts > %s ORDER BY source_ts",
            (tag_id, now - dt.timedelta(seconds=lookback_s)))
        history = cur.fetchall()

    if not history:
        # Never seen is a different state from seen and gone quiet.
        health = Health(tag_id, tag_name, now, None, is_missing=True,
                        computed_quality=STALE,
                        detail=f"no samples in the last {lookback_s:.0f}s")
        return [], health

    flags: list[Flag] = []
    last_ts, last_value, last_quality = history[-1]

    def add(rule: str, verdict: tuple[int, str] | None,
            at: dt.datetime | None = None) -> None:
        if verdict is not None:
            flags.append(Flag(tag_id, tag_name, at or last_ts, rule,
                              verdict[0], verdict[1]))

    # 1. range -- evaluated on every sample in the window, because an excursion
    #    that has since returned still happened.
    if "range" in rules:
        for ts, value, _ in history:
            add("range", check_range(value, range_low, range_high), at=ts)

    # 2. rate of change
    if "rate_of_change" in rules:
        limit = rules["rate_of_change"].get("max_per_second")
        if limit is not None:
            for (t0, v0, _), (t1, v1, _) in zip(history, history[1:]):
                add("rate_of_change",
                    check_rate_of_change(v1, v0, (t1 - t0).total_seconds(),
                                         float(limit)), at=t1)

    # 3. cross-tag consistency, gated on operating state.
    #
    # A consistency relationship holds within a regime, not always. Feedwater
    # flow tracks generation while the unit is on load; during a cold start-up
    # the boiler is being filled with the breaker open, and the same rule would
    # fire on every start-up for an hour. A check that cries wolf through every
    # normal evolution gets switched off, so the regime is part of the rule.
    if "cross_tag" in rules:
        params = {**DEFAULTS["cross_tag"], **rules["cross_tag"]}
        other_name = params.get("other_tag")
        gate_tag, gate_min = params.get("gate_tag"), params.get("gate_min")
        in_regime = True
        if gate_tag is not None and gate_min is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT s.value FROM sample s JOIN tag t ON t.id = s.tag_id"
                    " WHERE t.name = %s AND s.source_ts <= %s"
                    " ORDER BY s.source_ts DESC LIMIT 1", (gate_tag, last_ts))
                gate = cur.fetchone()
            in_regime = (gate is not None and gate[0] is not None
                         and gate[0] >= float(gate_min))
        if other_name and in_regime:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT s.value FROM sample s JOIN tag t ON t.id = s.tag_id"
                    " WHERE t.name = %s AND s.source_ts <= %s"
                    " ORDER BY s.source_ts DESC LIMIT 1", (other_name, last_ts))
                found = cur.fetchone()
            if found is not None:
                add("cross_tag",
                    check_cross_tag(last_value, found[0], other_name=other_name,
                                    ratio=float(params["ratio"]),
                                    tolerance=float(params["tolerance"])))

    # 4a. frozen
    if "frozen" in rules:
        params = {**DEFAULTS["frozen"], **rules["frozen"]}
        add("frozen", check_frozen([(ts, v) for ts, v, _ in history],
                                   float(params["window_seconds"]),
                                   float(params["tolerance"])))

    # 4b. stale
    if "stale" in rules:
        params = {**DEFAULTS["stale"], **rules["stale"]}
        add("stale", check_stale(last_ts, now, float(params["max_seconds"])))

    by_rule = {f.rule_type: f for f in flags}
    computed = max((f.computed_quality for f in flags),
                   key=severity, default=GOOD)

    health = Health(
        tag_id=tag_id, tag_name=tag_name, evaluated_at=now,
        last_source_ts=last_ts,
        # Source quality, not computed. These are different facts.
        is_bad=severity(last_quality) == 2,
        is_stale="stale" in by_rule,
        is_frozen="frozen" in by_rule,
        is_missing=False,
        is_out_of_range=any(f.rule_type == "range" and f.source_ts == last_ts
                            for f in flags),
        is_comm_failed=last_quality == int(ua.StatusCodes.BadNoCommunication),
        source_quality=last_quality,
        computed_quality=computed,
        # A standing note on the tag (e.g. "uncalibrated - bench demo only")
        # comes first: it is true of every value, whatever the rules found.
        detail="; ".join(([quality_note] if quality_note else [])
                         + [f"{f.rule_type}: {f.reason}" for f in by_rule.values()])
        or None,
    )
    return flags, health


def persist(conn: psycopg.Connection, flags: list[Flag], health: Health) -> None:
    with conn.cursor() as cur:
        if flags:
            cur.executemany(
                "INSERT INTO quality_flag (tag_id, source_ts, rule_type,"
                " computed_quality, reason) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (tag_id, source_ts, rule_type) DO UPDATE SET"
                " computed_quality = EXCLUDED.computed_quality,"
                " reason = EXCLUDED.reason",
                [(f.tag_id, f.source_ts, f.rule_type, f.computed_quality,
                  f.reason) for f in flags])
        cur.execute(
            "INSERT INTO tag_health (tag_id, evaluated_at, last_source_ts,"
            " is_bad, is_stale, is_frozen, is_missing, is_out_of_range,"
            " is_comm_failed, source_quality, computed_quality, detail)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (tag_id) DO UPDATE SET"
            " evaluated_at = EXCLUDED.evaluated_at,"
            " last_source_ts = EXCLUDED.last_source_ts,"
            " is_bad = EXCLUDED.is_bad, is_stale = EXCLUDED.is_stale,"
            " is_frozen = EXCLUDED.is_frozen, is_missing = EXCLUDED.is_missing,"
            " is_out_of_range = EXCLUDED.is_out_of_range,"
            " is_comm_failed = EXCLUDED.is_comm_failed,"
            " source_quality = EXCLUDED.source_quality,"
            " computed_quality = EXCLUDED.computed_quality,"
            " detail = EXCLUDED.detail",
            (health.tag_id, health.evaluated_at, health.last_source_ts,
             health.is_bad, health.is_stale, health.is_frozen, health.is_missing,
             health.is_out_of_range, health.is_comm_failed,
             health.source_quality, health.computed_quality, health.detail))
    conn.commit()


def evaluate_all(conn: psycopg.Connection, *, now: dt.datetime | None = None,
                 source_system: str = "opcua") -> list[Health]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tag WHERE source_system = %s ORDER BY name",
                    (source_system,))
        tag_ids = [r[0] for r in cur.fetchall()]
    out = []
    for tag_id in tag_ids:
        flags, health = evaluate_tag(conn, tag_id, now=now)
        persist(conn, flags, health)
        out.append(health)
    return out
