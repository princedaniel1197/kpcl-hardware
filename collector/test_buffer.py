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
           quality: int = 0) -> Sample:
    return Sample(tag_id=tag_id, tag_name=f"T{tag_id}",
                  source_ts=T0 + dt.timedelta(seconds=n),
                  server_ts=T0 + dt.timedelta(seconds=n, milliseconds=40),
                  value=(n * 1.0 if value is _UNSET else value),
                  quality=quality, seq=n)


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
