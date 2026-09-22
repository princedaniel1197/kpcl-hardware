"""Local store-and-forward buffer.

Losing the uplink must not stop acquisition (§319). When a forward fails,
samples land here and the subscription keeps running. On restore they are
replayed in ascending source-timestamp order, carrying their original
timestamps and quality, through an upsert that cannot duplicate (§382, §648).

SQLite, on disk, so a collector restart does not lose what it was holding.

Bounded, as the build plan requires. When the buffer is full the OLDEST rows are
discarded — a ring, which is how PI's buffering behaves. The alternative,
refusing new samples, would mean the most recent state of the plant is the part
you lose, which is worse for an operator watching a fault develop. Either way it
is a loss: it is logged, counted, and left detectable downstream through the
per-tag sequence numbers, because a loss you cannot see is the only kind that
actually hurts.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from collector.model import Sample

SCHEMA = """
CREATE TABLE IF NOT EXISTS buffered (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_id    INTEGER NOT NULL,
    tag_name  TEXT    NOT NULL,
    source_ts TEXT    NOT NULL,   -- ISO 8601 with offset, never naive
    server_ts TEXT    NOT NULL,
    value     REAL,               -- NULL when quality is Bad
    quality   INTEGER NOT NULL,
    seq       INTEGER NOT NULL,
    UNIQUE (tag_id, source_ts)
);
-- The drain reads in ascending source_ts, so that is what gets the index.
CREATE INDEX IF NOT EXISTS buffered_source_ts ON buffered (source_ts, id);
"""


class Buffer:
    def __init__(self, path: str | Path, max_rows: int = 500_000,
                 warn_fraction: float = 0.80) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = max_rows
        self.warn_fraction = warn_fraction
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self.overflowed = 0
        self._warned = False

    # -- writing -----------------------------------------------------------

    def append(self, samples: list[Sample]) -> int:
        """Buffer samples. Returns how many rows were discarded to make room."""
        if not samples:
            return 0
        self._conn.execute("BEGIN")
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO buffered "
                "(tag_id, tag_name, source_ts, server_ts, value, quality, seq) "
                "VALUES (?,?,?,?,?,?,?)",
                [(s.tag_id, s.tag_name, s.source_ts.isoformat(),
                  s.server_ts.isoformat(), s.value, s.quality, s.seq)
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
        depth = self._depth()
        if depth <= self.max_rows:
            return 0
        excess = depth - self.max_rows
        self._conn.execute(
            "DELETE FROM buffered WHERE id IN "
            "(SELECT id FROM buffered ORDER BY id LIMIT ?)", (excess,))
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

        Ascending source_ts is the whole point: the archive must never receive
        out-of-order data during recovery (§382).
        """
        rows = self._conn.execute(
            "SELECT id, tag_id, tag_name, source_ts, server_ts, value, quality, seq "
            "FROM buffered ORDER BY source_ts, id LIMIT ?", (limit,)).fetchall()
        return [
            (row[0], Sample(
                tag_id=row[1], tag_name=row[2],
                source_ts=dt.datetime.fromisoformat(row[3]),
                server_ts=dt.datetime.fromisoformat(row[4]),
                value=row[5], quality=row[6], seq=row[7]))
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

    def close(self) -> None:
        self._conn.close()
