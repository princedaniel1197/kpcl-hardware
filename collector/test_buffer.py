"""Buffer tests."""

from __future__ import annotations

import datetime as dt

from collector.buffer import Buffer
from collector.model import Sample

T0 = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
BAD = 2156593152


# None is a meaningful value here -- it is what a Bad sample carries -- so
# "not supplied" needs a sentinel of its own.
_UNSET = object()


def sample(n: int, tag_id: int = 1, value: float | None = _UNSET,
           quality: int = 0, run: int | None = 7) -> Sample:
    return Sample(tag_id=tag_id, tag_name=f"T{tag_id}",
                  source_ts=T0 + dt.timedelta(seconds=n),
                  server_ts=T0 + dt.timedelta(seconds=n, milliseconds=40),
                  value=(n * 1.0 if value is _UNSET else value),
                  quality=quality, seq=n, run=run)


def test_drain_order_is_ascending_source_ts(tmp_path):
    """The archive must never receive out-of-order data during recovery."""
    buf = Buffer(tmp_path / "b.sqlite")
    # Buffered out of order on purpose.
    buf.append([sample(5), sample(1), sample(9), sample(3)])
    taken = [s.source_ts for _, s in buf.take(10)]
    assert taken == sorted(taken)
    assert [s.seq for _, s in buf.take(10)] == [1, 3, 5, 9]


def test_original_timestamps_and_quality_survive_the_round_trip(tmp_path):
    buf = Buffer(tmp_path / "b.sqlite")
    original = sample(1, value=None, quality=BAD)
    buf.append([original])
    _, restored = buf.take(1)[0]
    assert restored.source_ts == original.source_ts
    assert restored.server_ts == original.server_ts
    assert restored.source_ts != restored.server_ts
    assert restored.value is None          # Bad carries no value
    assert restored.quality == BAD
    assert restored.seq == original.seq
    assert restored.run == original.run


def test_buffering_the_same_sample_twice_does_not_duplicate(tmp_path):
    buf = Buffer(tmp_path / "b.sqlite")
    buf.append([sample(1)])
    buf.append([sample(1)])
    assert buf.depth == 1


def test_bound_is_enforced_and_loss_is_counted(tmp_path):
    buf = Buffer(tmp_path / "b.sqlite", max_rows=10)
    buf.append([sample(n) for n in range(15)])
    assert buf.depth == 10
    assert buf.overflowed == 5
    # The oldest went; the most recent plant state is what remains.
    assert [s.seq for _, s in buf.take(10)] == list(range(5, 15))


def test_high_water_warns_once(tmp_path):
    buf = Buffer(tmp_path / "b.sqlite", max_rows=10, warn_fraction=0.8)
    buf.append([sample(n) for n in range(7)])
    assert buf.over_high_water() is False
    buf.append([sample(7)])
    assert buf.over_high_water() is True
    assert buf.over_high_water() is False   # not again while still high


def test_forget_only_removes_what_was_confirmed(tmp_path):
    buf = Buffer(tmp_path / "b.sqlite")
    buf.append([sample(n) for n in range(5)])
    batch = buf.take(3)
    buf.forget([row_id for row_id, _ in batch])
    assert buf.depth == 2
    assert [s.seq for _, s in buf.take(10)] == [3, 4]


def test_buffer_survives_reopen(tmp_path):
    path = tmp_path / "b.sqlite"
    buf = Buffer(path)
    buf.append([sample(n) for n in range(4)])
    buf.close()
    reopened = Buffer(path)
    assert reopened.depth == 4


def test_bound_records_what_it_discarded_per_tag(tmp_path):
    """The loss is written down, per tag and run, with the sequence numbers
    and the source-time span discarded -- in the same transaction as the
    discard, so a crash cannot lose the record of a loss."""
    buf = Buffer(tmp_path / "b.sqlite", max_rows=10)
    buf.append([sample(n, tag_id=1) for n in range(6)]
               + [sample(n, tag_id=2) for n in range(6)])
    # 12 rows against a bound of 10: the two oldest by insertion go.
    losses = {loss["tag_id"]: loss for _, loss in buf.losses()}
    assert buf.pending_losses() == 1
    lost = losses[1]
    assert (lost["first_seq"], lost["last_seq"], lost["samples"]) == (0, 1, 2)
    assert lost["first_source_ts"] == T0
    assert lost["last_source_ts"] == T0 + dt.timedelta(seconds=1)
    assert lost["run"] == 7 and lost["reason"] == "buffer_overflow"
    buf.forget_losses([row_id for row_id, _ in buf.losses()])
    assert buf.pending_losses() == 0


def test_timestamps_are_exact_to_the_microsecond(tmp_path):
    buf = Buffer(tmp_path / "b.sqlite")
    when = dt.datetime(2026, 9, 23, 1, 2, 3, 999_999, tzinfo=dt.timezone.utc)
    buf.append([Sample(1, "T1", when, when + dt.timedelta(microseconds=1), 1.0, 0)])
    _, restored = buf.take(1)[0]
    assert restored.source_ts == when
    assert restored.server_ts == when + dt.timedelta(microseconds=1)


def test_drain_order_does_not_depend_on_how_a_timestamp_is_written(tmp_path):
    """Integer microseconds sort chronologically whatever the offset or the
    number of fractional digits a timestamp was created with. ISO text did
    not: '...:03+05:30' sorts after '...:02+00:00' though it is hours earlier."""
    buf = Buffer(tmp_path / "b.sqlite")
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    early = dt.datetime(2026, 9, 23, 5, 30, 3, tzinfo=ist)       # 00:00:03 UTC
    late = dt.datetime(2026, 9, 23, 0, 0, 4, tzinfo=dt.timezone.utc)
    buf.append([Sample(1, "T1", late, None, 1.0, 0),
                Sample(2, "T2", early, None, 2.0, 0)])
    assert [s.tag_id for _, s in buf.take(10)] == [2, 1]


def test_a_missing_server_timestamp_stays_missing(tmp_path):
    buf = Buffer(tmp_path / "b.sqlite")
    buf.append([Sample(1, "T1", T0, None, 1.0, 0)])
    _, restored = buf.take(1)[0]
    assert restored.server_ts is None


def test_durability_is_full(tmp_path):
    buf = Buffer(tmp_path / "b.sqlite")
    # 2 = FULL
    assert buf._conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_a_version_one_buffer_is_upgraded_without_losing_rows(tmp_path):
    """A buffer is non-empty only after an outage, which is when its contents
    matter most. Upgrading the file format must carry every row across."""
    import sqlite3
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript("""
        CREATE TABLE buffered (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tag_id INTEGER NOT NULL,
            tag_name TEXT NOT NULL, source_ts TEXT    NOT NULL,
            server_ts TEXT    NOT NULL, value REAL, quality INTEGER NOT NULL,
            seq INTEGER NOT NULL, UNIQUE (tag_id, source_ts));
        CREATE INDEX buffered_source_ts ON buffered (source_ts, id);""")
    for n in (3, 1, 2):
        conn.execute("INSERT INTO buffered (tag_id, tag_name, source_ts,"
                     " server_ts, value, quality, seq) VALUES (1,'T1',?,?,?,0,?)",
                     ((T0 + dt.timedelta(seconds=n)).isoformat(),
                      (T0 + dt.timedelta(seconds=n, milliseconds=40)).isoformat(),
                      float(n), n))
    conn.close()
    buf = Buffer(path)
    rows = [s for _, s in buf.take(10)]
    assert [s.value for s in rows] == [1.0, 2.0, 3.0]
    assert rows[0].server_ts == T0 + dt.timedelta(seconds=1, milliseconds=40)
    # The old receipt-order counter meant nothing downstream; it is not kept.
    assert all(s.seq is None and s.run is None for s in rows)
