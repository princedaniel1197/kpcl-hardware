"""Session tests: what becomes a sample, what is refused, and how loss before
the collector is detected.

The last three tests run a real simulator in-process and connect the real
session to it over a real socket. Two of them are evidence as much as tests:
one shows that a lost NotificationMessage is detected from the server's own
numbering, and one pins down the asyncua server defect that decides
config/sources.json. If asyncua ever fixes that defect, that test fails, and
the setting should be revisited rather than the test edited.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace

import pytest
from asyncua import ua

from collector.session import (IngressCounts, PublishSequence, ReadOnlySession,
                               _SubscriptionHandler)
from sim.server import APPLICATION_URI, SimulatorServer

logging.getLogger("asyncua").setLevel(logging.ERROR)

T0 = dt.datetime(2026, 9, 23, 1, 0, tzinfo=dt.timezone.utc)
BAD = int(ua.StatusCodes.BadDeviceFailure)


# -- the server's numbering ---------------------------------------------------

def test_continuous_numbering_is_not_a_gap():
    seq = PublishSequence()
    assert [seq.observe(1, n, False) for n in (1, 2, 3, 4)] == [0, 0, 0, 0]


def test_a_hole_in_data_messages_is_counted():
    seq = PublishSequence()
    seq.observe(1, 1, False)
    seq.observe(1, 2, False)
    assert seq.observe(1, 5, False) == 2          # 3 and 4 never arrived


def test_a_keep_alive_reveals_a_hole_before_the_next_data_message():
    """A keep-alive carries the number the NEXT data message will use. One
    that is ahead of last+1 means messages in between were lost."""
    seq = PublishSequence()
    seq.observe(1, 7, False)
    assert seq.observe(1, 8, True) == 0           # next will be 8: continuous
    assert seq.observe(1, 10, True) == 2          # 8 and 9 are gone
    assert seq.observe(1, 10, False) == 0         # and not counted twice


def test_a_republished_message_is_not_a_gap():
    seq = PublishSequence()
    seq.observe(1, 4, False)
    assert seq.observe(1, 3, False) == 0


def test_subscriptions_are_numbered_independently():
    seq = PublishSequence()
    seq.observe(1, 10, False)
    assert seq.observe(2, 1, False) == 0
    assert seq.observe(1, 11, False) == 0


# -- what becomes a sample ---------------------------------------------------

def _handler():
    got, dropped = [], []
    counts = IngressCounts()
    tag = {"id": 5, "name": "T5"}
    handler = _SubscriptionHandler(got.append, {"node": tag}, counts,
                                   lambda name, why: dropped.append((name, why)))
    return handler, got, dropped, counts


def _dv(value=1.5, status=0, source=T0, server=T0 + dt.timedelta(milliseconds=40)):
    return SimpleNamespace(
        Value=None if value is None else SimpleNamespace(Value=value),
        StatusCode=None if status is None else ua.StatusCode(status),
        SourceTimestamp=source, ServerTimestamp=server)


def test_a_good_datavalue_becomes_a_sample_with_both_timestamps():
    handler, got, _, _ = _handler()
    handler.handle("node", _dv())
    (s,) = got
    assert (s.value, s.quality, s.source_ts, s.server_ts) == (
        1.5, 0, T0, T0 + dt.timedelta(milliseconds=40))


def test_no_source_timestamp_is_refused_and_counted():
    handler, got, dropped, counts = _handler()
    handler.handle("node", _dv(source=None))
    assert got == [] and counts.dropped_no_source_ts == 1
    assert dropped == [("T5", "DataValue carried no SourceTimestamp")]


def test_no_status_code_is_refused_not_assumed_good():
    """Assuming Good for a missing quality substitutes the most favourable
    quality there is."""
    handler, got, _, counts = _handler()
    handler.handle("node", _dv(status=None))
    assert got == [] and counts.dropped_no_status == 1


def test_an_unstorable_value_is_refused_not_stored_empty_and_good():
    handler, got, _, counts = _handler()
    handler.handle("node", _dv(value="OPEN"))
    assert got == [] and counts.dropped_unstorable == 1


def test_no_server_timestamp_is_stored_as_none_never_as_source_time():
    handler, got, _, counts = _handler()
    handler.handle("node", _dv(server=None))
    (s,) = got
    assert s.server_ts is None and s.source_ts == T0
    assert counts.no_server_ts == 1


def test_a_bad_datavalue_keeps_its_code_and_carries_no_value():
    handler, got, _, _ = _handler()
    handler.handle("node", _dv(value=None, status=BAD))
    (s,) = got
    assert s.value is None and s.quality == BAD


def test_a_digital_is_stored_as_one_or_zero():
    handler, got, _, _ = _handler()
    handler.handle("node", _dv(value=True))
    handler.handle("node", _dv(value=False, source=T0 + dt.timedelta(seconds=1)))
    assert [s.value for s in got] == [1.0, 0.0]


# -- against a real server ---------------------------------------------------

ENDPOINT = "opc.tcp://127.0.0.1:48450/orianode/crpms/session-test/"
SCAN_MS = 100.0


@pytest.fixture
async def sim():
    server = SimulatorServer(endpoint=ENDPOINT, startup_seconds=8.0,
                             scan_interval_ms=SCAN_MS, field_latency_ms=10.0,
                             seed=11)
    await server.init()
    async with server._server:
        server.restart()

        async def scan() -> None:
            while True:
                await server.scan_once()
                await asyncio.sleep(SCAN_MS / 1000.0)

        task = asyncio.create_task(scan())
        await asyncio.sleep(0.3)
        try:
            yield server
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _tags(*names: str, exc_dev: float | None = None) -> list[dict]:
    return [{"id": i, "name": n, "scan_rate_ms": 100, "exc_dev": exc_dev,
             "comp_dev": None, "max_time_ms": 60000, "source_path": "Unit1"}
            for i, n in enumerate(names, start=1)]


async def test_the_server_is_recognised_and_its_setting_applied(sim):
    got = []
    session = ReadOnlySession(ENDPOINT, got.append)
    await session.connect(_tags("U1_MS_PRESS"))
    try:
        await asyncio.sleep(0.5)
        assert session.server_uri == APPLICATION_URI
        # config/sources.json: no deadband at this source, deliberately.
        assert session.source_deadband is False
        assert got, "no samples arrived"
    finally:
        await session.disconnect()


async def test_a_tag_with_no_source_path_is_refused_loudly(sim):
    session = ReadOnlySession(ENDPOINT, lambda s: None)
    tags = _tags("U1_MS_PRESS", "U1_MW")
    tags[1]["source_path"] = None
    await session.connect(tags)
    try:
        assert session.unresolved == ["U1_MW"]
        assert session.counts.unresolved_tags == 1
        assert session.subscribed == ["U1_MS_PRESS"]
    finally:
        await session.disconnect()


async def test_a_lost_notification_message_is_detected(sim):
    """Drop one data-carrying PublishResponse between the client and the
    session's check -- which is what a lost message looks like from here --
    and the server's own numbering must reveal it."""
    gaps = []
    session = ReadOnlySession(ENDPOINT, lambda s: None,
                              on_publish_gap=lambda *a: gaps.append(a))
    await session.connect(_tags("U1_MS_PRESS", "U1_DRUM_PRESS"))
    try:
        subscription = session._subscriptions[0]
        registry = subscription.server._subscription_callbacks
        checked = registry[subscription.subscription_id]
        seen = {"data": 0}

        async def lossy(result: ua.PublishResult) -> None:
            if result.NotificationMessage.NotificationData:
                seen["data"] += 1
                if seen["data"] == 3:
                    return                      # this message is lost
            await checked(result)

        registry[subscription.subscription_id] = lossy
        await asyncio.sleep(1.5)
        assert seen["data"] > 4
        assert session.counts.publish_missed == 1
        assert session.counts.publish_gaps == 1
        assert gaps and gaps[0][2] == 1
    finally:
        await session.disconnect()


async def test_asyncua_server_hides_a_status_change_inside_the_deadband(sim):
    """The measurement config/sources.json rests on.

    One tag, subscribed twice: once with a source deadband wider than anything
    the value will do, once with none. Force it Bad. A server that honours the
    StatusValue trigger reports the status change on both; asyncua 2.0.1
    reports it only on the subscription without a deadband."""
    await asyncio.sleep(1.0)
    with_db, without = [], []
    on = ReadOnlySession(ENDPOINT, with_db.append, source_deadband=True)
    off = ReadOnlySession(ENDPOINT, without.append, source_deadband=False)
    await on.connect(_tags("U1_MS_PRESS", exc_dev=100_000.0))
    await off.connect(_tags("U1_MS_PRESS", exc_dev=100_000.0))
    try:
        await asyncio.sleep(0.5)
        sim.overrides.force("U1_MS_PRESS", "BadDeviceFailure")
        await asyncio.sleep(1.5)
        bad_with = [s for s in with_db if (s.quality >> 30) & 3 == 2]
        bad_without = [s for s in without if (s.quality >> 30) & 3 == 2]
        assert bad_without, "without a source deadband the Bad status arrived"
        assert bad_with == [], (
            "asyncua now reports a status change inside the deadband; the "
            "source_deadband setting for the simulator can be revisited")
    finally:
        sim.overrides.clear_all()
        await on.disconnect()
        await off.disconnect()
