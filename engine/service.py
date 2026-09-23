"""The engine, running: KPIs, quality rules and event frames, continuously.

Until 23 September 2026 nothing ran these. KPIs, quality verdicts and event
frames were produced only while a demonstration script was running, so the
dashboard's KPI strip and Health tab showed whatever the last demonstration had
left behind -- twelve hours old when it was noticed -- and the Stage 12 claim
that a Bad value is seen to be rejected at the KPI node depended on someone
having just run kpi_demo. This is the service that makes those claims true of
the running system.

Each cycle:

  * every current KPI definition, at its own calculation_freq_ms, for every
    element it applies to (below);
  * every quality rule, for every acquired tag, every QUALITY_INTERVAL_S;
  * event-frame detection over a window reaching back one template
    max_duration, every EVENTS_INTERVAL_S -- detection is batch over the
    archive and idempotent on (element, template, start), so re-running it
    completes an open frame rather than duplicating it.

WHICH ELEMENTS A KPI APPLIES TO is derived, not configured: the lowest elements
whose subtree holds every attribute the definition needs. Heat rate needs
CoalFlow and GrossGeneration, which a Unit's subtree holds and a Boiler's does
not; the Station's subtree holds them too, but through a Unit, so the Unit is
the one it applies to. Adding a unit is therefore still configuration (rule 7).
A unit that exists in the model but has no source -- Unit 2 here -- gets KPIs
that are Bad, naming inputs with no value. That is the truth about it.

WHAT IT DOES NOT DO. It computes KPIs for now, not retrospectively: after a
database outage, the KPI record has a gap for the outage even though the
samples were buffered and replayed. Recomputing history is a deliberate
operation (`--backfill` is not implemented), because a KPI computed late from
replayed data and one computed live are different facts about the system.

    python -m engine
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import time
from pathlib import Path

import psycopg

from engine import events, kpi, quality

log = logging.getLogger("engine")

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
TEMPLATES = Path(__file__).parent.parent / "config" / "event_templates.json"
QUALITY_INTERVAL_S = float(os.environ.get("ENGINE_QUALITY_INTERVAL_S", "10"))
EVENTS_INTERVAL_S = float(os.environ.get("ENGINE_EVENTS_INTERVAL_S", "30"))
TICK_S = 1.0
CLASS = ("Good", "Uncertain", "Bad", "Reserved")


def applicable_elements(conn: psycopg.Connection, attributes: set[str]) -> list[int]:
    """The lowest elements whose subtree holds every one of `attributes`."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, parent_id FROM element")
        parent = {i: p for i, p in cur.fetchall()}
        cur.execute("SELECT element_id, name FROM attribute")
        own: dict[int, set[str]] = {}
        for element_id, name in cur.fetchall():
            own.setdefault(element_id, set()).add(name)
    subtree: dict[int, set[str]] = {i: set(own.get(i, ())) for i in parent}
    for element_id, names in own.items():
        up = parent.get(element_id)
        while up is not None:
            subtree[up] |= names
            up = parent.get(up)
    candidates = {i for i, names in subtree.items() if attributes <= names}

    def has_candidate_below(element_id: int) -> bool:
        return any(c != element_id and _is_ancestor(parent, element_id, c)
                   for c in candidates)

    return sorted(c for c in candidates if not has_candidate_below(c))


def _is_ancestor(parent: dict[int, int | None], ancestor: int, element_id: int) -> bool:
    up = parent.get(element_id)
    while up is not None:
        if up == ancestor:
            return True
        up = parent.get(up)
    return False


class Engine:
    def __init__(self, dsn: str = DSN) -> None:
        self.dsn = dsn
        self._conn: psycopg.Connection | None = None
        self._kpi_due: dict[int, float] = {}
        self._quality_due = 0.0
        self._events_due = 0.0
        self._events_ran: dict[tuple[str, int], dt.datetime] = {}
        self._kpi_state: dict[tuple[str, int], tuple[int, str | None]] = {}
        self.templates = events.load_templates(TEMPLATES)
        self.stats = {"kpi_results": 0, "quality_runs": 0, "frames_stored": 0,
                      "db_errors": 0}

    def connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, connect_timeout=5)
        return self._conn

    def discard(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
        self._conn = None

    # -- the three jobs ------------------------------------------------------

    def run_kpis(self, now: float) -> None:
        conn = self.connection()
        for definition in kpi.load_definitions(conn):
            if now < self._kpi_due.get(definition.id, 0.0):
                continue
            self._kpi_due[definition.id] = now + definition.calculation_freq_ms / 1000
            for element_id in applicable_elements(conn, set(definition.inputs.values())):
                result = kpi.compute(conn, definition, element_id)
                kpi.store(conn, result)
                self.stats["kpi_results"] += 1
                # Logged when the verdict changes, not on every cycle.
                state = (result.quality, result.reason)
                key = (definition.name, element_id)
                if self._kpi_state.get(key) != state:
                    self._kpi_state[key] = state
                    log.info("%s on element %d: %s%s", definition.name, element_id,
                             CLASS[kpi.severity(result.quality)],
                             f" ({result.reason})" if result.reason else "")

    def run_quality(self, now: float) -> None:
        if now < self._quality_due:
            return
        self._quality_due = now + QUALITY_INTERVAL_S
        quality.evaluate_all(self.connection())
        self.stats["quality_runs"] += 1

    def run_events(self, now: float) -> None:
        if now < self._events_due:
            return
        self._events_due = now + EVENTS_INTERVAL_S
        conn = self.connection()
        until = dt.datetime.now(dt.timezone.utc)
        margin = dt.timedelta(minutes=10)
        for template in self.templates.values():
            needed = {template.start.attribute, template.end.attribute}
            needed |= {m.trigger.attribute for m in template.milestones}
            longest = until - dt.timedelta(seconds=template.max_duration_s) - margin
            # A frame left open for longer than its template allows will never
            # see its end in any window this service reads. It is closed as
            # aborted -- the same verdict detect() gives a frame that overruns
            # -- rather than left to block the next frame from opening.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE event_frame SET status = 'aborted',"
                    " end_ts = start_ts + make_interval(secs => %s)"
                    " WHERE template = %s AND end_ts IS NULL AND start_ts < %s",
                    (template.max_duration_s, template.name, longest))
            conn.commit()
            for element_id in applicable_elements(conn, needed):
                # The first pass looks back one whole template duration. After
                # that, from shortly before the last pass -- or from the start
                # of a frame still open, so it can be closed -- rather than
                # re-reading hours of history every thirty seconds.
                since = longest
                ran = self._events_ran.get((template.name, element_id))
                if ran is not None:
                    since = max(longest, ran - margin)
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT min(start_ts) FROM event_frame WHERE"
                            " element_id = %s AND template = %s AND end_ts IS NULL"
                            " AND start_ts >= %s", (element_id, template.name, longest))
                        open_start = cur.fetchone()[0]
                    if open_start is not None:
                        since = min(since, open_start - dt.timedelta(minutes=1))
                for frame in events.detect(conn, template, element_id, since, until):
                    events.store(conn, frame)
                    self.stats["frames_stored"] += 1
                self._events_ran[(template.name, element_id)] = until

    # -- the loop ------------------------------------------------------------

    def run(self, stop) -> None:
        log.info("engine running: KPIs at their own frequency, quality every "
                 "%.0f s, event frames every %.0f s", QUALITY_INTERVAL_S,
                 EVENTS_INTERVAL_S)
        last_error: str | None = None
        while not stop():
            now = time.monotonic()
            for job in (self.run_kpis, self.run_quality, self.run_events):
                try:
                    job(now)
                    last_error = None
                except psycopg.Error as exc:
                    # The archive being away stops the engine computing; it must
                    # not stop the engine. It resumes when the archive returns.
                    self.stats["db_errors"] += 1
                    message = str(exc).strip().splitlines()[0]
                    if message != last_error:
                        log.warning("archive unavailable, %s waits: %s",
                                    job.__name__, message)
                        last_error = message
                    self.discard()
                    break
            time.sleep(TICK_S)
        self.discard()
        log.info("engine stopped: %s", self.stats)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engine")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout)
    logging.Formatter.converter = time.gmtime

    stopping = {"now": False}

    def request_stop(signum, frame) -> None:
        stopping["now"] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    Engine().run(lambda: stopping["now"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
