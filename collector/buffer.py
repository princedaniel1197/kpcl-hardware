"""Local store-and-forward buffer.

Losing the uplink must not stop acquisition (§319). When a forward fails,
samples land here and the subscription keeps running. On restore they are
replayed in ascending source-timestamp order, carrying their original
timestamps and quality, through an upsert that cannot duplicate (§382, §648).

SQLite, on disk, so a collector restart does not lose what it was holding.

DURABILITY. WAL with `synchronous=FULL`: a buffered row is on disk when append()
returns, including across a power cut. `NORMAL` would be faster and, in WAL
mode, can lose the last transactions on power loss -- and this buffer exists
precisely for the moment something has failed. At the rates here the cost of
FULL is not measurable against the work around it.

TIMESTAMPS ARE INTEGERS: microseconds since the Unix epoch, UTC. The drain reads
in source-timestamp order, and an integer column sorts chronologically by
definition. (The first version stored ISO-8601 text and relied on every string
having the same offset and the same number of fractional digits for lexical
order to equal time order. That held, and was one refactor from not holding.)
Microseconds are exactly what Python's datetime carries, so nothing is rounded.

Bounded, as the build plan requires. When the buffer is full the OLDEST rows are
discarded — a ring, which is how PI's buffering behaves. The alternative,
refusing new samples, would mean the most recent state of the plant is the part
you lose, which is worse for an operator watching a fault develop. Either way it
is a loss, and it is not merely logged: every discarded range is written to the
`lost` table here -- per tag, with its sequence numbers and source-timestamp
span -- and forwarded to the archive's `collector_loss` table when the link
allows, so the loss can be found and reconciled by query.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from collector.model import Sample

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS buffered (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_id    INTEGER NOT NULL,
    tag_name  TEXT    NOT NULL,
    source_us INTEGER NOT NULL,   -- microseconds since epoch, UTC
    server_us INTEGER,            -- NULL when no server stamped the value
    value     REAL,               -- NULL when quality is Bad
    quality   INTEGER NOT NULL,
    run       INTEGER,
    seq       INTEGER,
    UNIQUE (tag_id, source_us)
);
-- The drain reads in ascending source time, so that is what gets the index.
CREATE INDEX IF NOT EXISTS buffered_source ON buffered (source_us, id);

-- Ranges discarded when the buffer overflowed, awaiting forward to the archive.
CREATE TABLE IF NOT EXISTS lost (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run          INTEGER,
    tag_id       INTEGER NOT NULL,
    first_us     INTEGER NOT NULL,
    last_us      INTEGER NOT NULL,
    first_seq    INTEGER,
    last_seq     INTEGER,
    samples      INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    detected_us  INTEGER NOT NULL
);
"""

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def to_us(when: dt.datetime) -> int:
    """Exact: integer arithmetic on the timedelta, no float in between."""
    if when.tzinfo is None:
        raise ValueError("buffered timestamps must be timezone-aware")
    delta = when - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_us(us: int) -> dt.datetime:
    return _EPOCH + dt.timedelta(microseconds=us)


def _statements(script: str) -> list[str]:
    """Split the schema into statements. It contains no string literals with
    semicolons, which is the only thing that would make this naive split
    wrong."""
    lines = [l for l in script.splitlines() if not l.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


class Buffer:
    def __init__(self, path: str | Path, max_rows: int = 500_000,
                 warn_fraction: float = 0.80) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = max_rows
        self.warn_fraction = warn_fraction
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._upgrade()
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.overflowed = 0
        self._warned = False

    def _upgrade(self) -> None:
        """Carry rows held by a version-1 buffer (ISO-8601 text timestamps,
        NOT NULL server_ts) into the current schema, in one transaction. A
        buffer is only ever non-empty after an outage, which is exactly when
        its contents matter most, so an upgrade must never discard them."""
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        has_v1 = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='buffered'"
            " AND sql LIKE '%source_ts TEXT%'").fetchone()
        if version >= SCHEMA_VERSION or not has_v1:
            return
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("ALTER TABLE buffered RENAME TO buffered_v1")
            self._conn.execute("DROP INDEX IF EXISTS buffered_source_ts")
            # executescript() would COMMIT first; each statement is executed
            # on its own so the whole upgrade is one transaction.
            for statement in _statements(SCHEMA):
                self._conn.execute(statement)
            rows = self._conn.execute(
                "SELECT tag_id, tag_name, source_ts, server_ts, value, quality"
                " FROM buffered_v1 ORDER BY id").fetchall()
            self._conn.executemany(
                "INSERT OR IGNORE INTO buffered (tag_id, tag_name, source_us,"
                " server_us, value, quality, run, seq)"
                " VALUES (?,?,?,?,?,?,NULL,NULL)",
                [(tag_id, name, to_us(dt.datetime.fromisoformat(src)),
                  to_us(dt.datetime.fromisoformat(srv)) if srv else None,
                  value, quality)
                 for tag_id, name, src, srv, value, quality in rows])
            self._conn.execute("DROP TABLE buffered_v1")
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    # -- writing -----------------------------------------------------------

    def append(self, samples: list[Sample]) -> int:
        """Buffer samples. Returns how many rows were discarded to make room."""
        if not samples:
            return 0
        self._conn.execute("BEGIN")
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO buffered "
                "(tag_id, tag_name, source_us, server_us, value, quality, run, seq)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(s.tag_id, s.tag_name, to_us(s.source_ts),
                  to_us(s.server_ts) if s.server_ts is not None else None,
                  s.value, s.quality, s.run, s.seq)
                 for s in samples],
            )
            discarded = self._enforce_bound()
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self.overflowed += discarded
        return discarded

    def _enforce_bound(self) -> int:
        """Discard the oldest rows beyond the bound, recording what was lost,
        per tag and run, in the same transaction as the discard."""
        depth = self._depth()
        if depth <= self.max_rows:
            return 0
        excess = depth - self.max_rows
        detected = to_us(dt.datetime.now(dt.timezone.utc))
        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS doomed (id INTEGER PRIMARY KEY)")
        self._conn.execute("DELETE FROM doomed")
        self._conn.execute(
            "INSERT INTO doomed SELECT id FROM buffered ORDER BY id LIMIT ?",
            (excess,))
        self._conn.execute(
            "INSERT INTO lost (run, tag_id, first_us, last_us, first_seq,"
            " last_seq, samples, reason, detected_us)"
            " SELECT run, tag_id, min(source_us), max(source_us), min(seq),"
            "  max(seq), count(*), 'buffer_overflow', ?"
            " FROM buffered WHERE id IN (SELECT id FROM doomed)"
            " GROUP BY run, tag_id", (detected,))
        self._conn.execute(
            "DELETE FROM buffered WHERE id IN (SELECT id FROM doomed)")
        return excess

    # -- reading -----------------------------------------------------------

    def _depth(self) -> int:
        return self._conn.execute("SELECT count(*) FROM buffered").fetchone()[0]

    @property
    def depth(self) -> int:
        return self._depth()

    @property
    def fraction_full(self) -> float:
        return self._depth() / self.max_rows if self.max_rows else 0.0

    def over_high_water(self) -> bool:
        """True the first time the buffer crosses its warning threshold, so the
        caller logs once rather than on every sample."""
        over = self.fraction_full >= self.warn_fraction
        if over and not self._warned:
            self._warned = True
            return True
        if not over:
            self._warned = False
        return False

    def take(self, limit: int) -> list[tuple[int, Sample]]:
        """The oldest `limit` samples by SOURCE timestamp.

        Ascending source time is the whole point: the archive must never
        receive out-of-order data during recovery (§382).
        """
        rows = self._conn.execute(
            "SELECT id, tag_id, tag_name, source_us, server_us, value, quality,"
            " run, seq FROM buffered ORDER BY source_us, id LIMIT ?",
            (limit,)).fetchall()
        return [
            (row[0], Sample(
                tag_id=row[1], tag_name=row[2],
                source_ts=from_us(row[3]),
                server_ts=from_us(row[4]) if row[4] is not None else None,
                value=row[5], quality=row[6], run=row[7], seq=row[8]))
            for row in rows
        ]

    def forget(self, ids: list[int]) -> None:
        """Drop rows once the archive has them. Called only after a successful
        commit, so a crash mid-drain replays rather than loses."""
        if not ids:
            return
        self._conn.execute("BEGIN")
        try:
            self._conn.executemany("DELETE FROM buffered WHERE id = ?",
                                   [(i,) for i in ids])
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # -- the loss ledger ----------------------------------------------------

    def pending_losses(self) -> int:
        return self._conn.execute("SELECT count(*) FROM lost").fetchone()[0]

    def losses(self) -> list[tuple[int, dict]]:
        rows = self._conn.execute(
            "SELECT id, run, tag_id, first_us, last_us, first_seq, last_seq,"
            " samples, reason, detected_us FROM lost ORDER BY id").fetchall()
        return [(r[0], {
            "run": r[1], "tag_id": r[2],
            "first_source_ts": from_us(r[3]), "last_source_ts": from_us(r[4]),
            "first_seq": r[5], "last_seq": r[6], "samples": r[7],
            "reason": r[8], "detected_at": from_us(r[9])}) for r in rows]

    def forget_losses(self, ids: list[int]) -> None:
        if not ids:
            return
        self._conn.execute("BEGIN")
        try:
            self._conn.executemany("DELETE FROM lost WHERE id = ?",
                                   [(i,) for i in ids])
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._conn.close()
