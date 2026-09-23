"""The OPC UA session.

THIS SESSION IS READ-ONLY. There is no write method in this module, none in
this package, and none behind a flag or a comment. That is an architectural
guarantee rather than a promise, and `test_readonly.py` asserts it by
inspecting the package's own source. (§303, §315)

Subscription, not polling: each tag is subscribed with its own sampling
interval, and with its deadband applied at the source where the source can be
trusted to apply it (see `source_deadband` below). The load placed on the source
is a configured quantity, not an assumption (§317).

LOSS BETWEEN THE SOURCE AND THE COLLECTOR is detected from the server's own
numbering. Every NotificationMessage an OPC UA server publishes on a
subscription carries a SequenceNumber the SERVER assigns, one higher than the
last; a keep-alive carries the number the next message will use (Part 4,
5.13.1). A hole in those numbers is a message the collector never received.
That is the standards-based check a PI interface makes, and it is the only one
that can see a loss: a counter the collector assigns to what it receives is
consecutive by construction, whatever went missing before it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from asyncua import Client, ua

from collector.config import source_settings
from collector.model import Sample

log = logging.getLogger("collector.session")


@dataclass
class IngressCounts:
    """What happened to DataValues that did NOT become samples, and to those
    that became samples missing something. Published as health tags (§462): a
    loss that lives only in a log file is a loss nobody sees."""

    dropped_no_source_ts: int = 0
    dropped_no_status: int = 0
    dropped_unstorable: int = 0
    no_server_ts: int = 0
    unresolved_tags: int = 0
    publish_gaps: int = 0
    publish_missed: int = 0
    by_tag: dict[str, int] = field(default_factory=dict)


class PublishSequence:
    """Continuity of the server-assigned NotificationMessage sequence numbers,
    per subscription.

    A data-carrying message must be numbered one higher than the last. A
    keep-alive carries the number the next data message will use, so a
    keep-alive numbered further ahead than that also reveals missed messages.
    Returns how many messages were missed; 0 when the numbering is continuous.
    """

    def __init__(self) -> None:
        self._last: dict[int, int] = {}

    def observe(self, subscription_id: int, sequence_number: int,
                keep_alive: bool) -> int:
        last = self._last.get(subscription_id)
        if keep_alive:
            # The next data message will be `sequence_number`.
            expected_next = sequence_number
            if last is None:
                self._last[subscription_id] = expected_next - 1
                return 0
            missed = max(0, expected_next - (last + 1))
            if missed:
                self._last[subscription_id] = expected_next - 1
            return missed
        if last is None:
            self._last[subscription_id] = sequence_number
            return 0
        if sequence_number <= last:
            # A republished message the collector has already seen. Not a gap.
            return 0
        missed = sequence_number - last - 1
        self._last[subscription_id] = sequence_number
        return missed

    def forget(self, subscription_id: int) -> None:
        self._last.pop(subscription_id, None)


class _SubscriptionHandler:
    """Receives data changes and turns them into Samples.

    asyncua calls `datachange_notification` with the raw DataValue, which is the
    only place the four quantities -- value, StatusCode, SourceTimestamp,
    ServerTimestamp -- are all available together. They are extracted here and
    travel onward as one object.
    """

    def __init__(self, on_sample: Callable[[Sample], None],
                 tag_by_node: dict, counts: IngressCounts,
                 on_drop: Callable[[str, str], None] | None = None) -> None:
        self._on_sample = on_sample
        self._tag_by_node = tag_by_node
        self._counts = counts
        self._on_drop = on_drop
        # When each tag was last heard from, for the max-time read below.
        self.last_seen: dict[int, float] = {}

    def datachange_notification(self, node, value, data) -> None:
        self.handle(node, data.monitored_item.Value)

    def _drop(self, counter: str, tag_name: str, why: str) -> None:
        setattr(self._counts, counter, getattr(self._counts, counter) + 1)
        self._counts.by_tag[tag_name] = self._counts.by_tag.get(tag_name, 0) + 1
        log.error("%s: %s; dropped", tag_name, why)
        if self._on_drop is not None:
            self._on_drop(tag_name, why)

    def handle(self, node, dv) -> None:
        """Turn one DataValue into a Sample, or refuse it and count why.

        Used by the subscription and by the periodic max-time read, so both
        paths extract the four quantities identically.
        """
        tag = self._tag_by_node.get(node)
        if tag is None:
            return
        name = tag["name"]

        # Never substitute receipt time for a valid SourceTimestamp (§335).
        # If the server sent none at all, the sample is unusable as a
        # measurement -- inventing one here would be indistinguishable from the
        # thing this project exists to prevent. Refused, and counted.
        if dv.SourceTimestamp is None:
            self._drop("dropped_no_source_ts", name,
                       "DataValue carried no SourceTimestamp")
            return

        # Quality is never assumed. On the wire an omitted StatusCode MEANS
        # Good (Part 6, DataValue encoding), and asyncua decodes it to
        # StatusCode(0) -- so a DataValue that reaches here with None has no
        # quality at all, which is a different thing from Good. Assuming Good
        # would substitute the most favourable quality there is (§318).
        if dv.StatusCode is None:
            self._drop("dropped_no_status", name,
                       "DataValue carried no StatusCode")
            return

        # A Bad DataValue carries no value (OPC UA Part 4, measured in Stage 1).
        # dv.Value.Value is then None and stays None.
        raw = dv.Value.Value if dv.Value is not None else None
        value, storable = _as_float(raw)
        if not storable:
            # A string or an array cannot be stored as a double. Storing the
            # sample with no value would pair "no value" with whatever quality
            # the source sent -- a Good sample with nothing in it. Refused.
            self._drop("dropped_unstorable", name,
                       f"value {raw!r} is not storable as a double")
            return

        source_ts = _as_utc(dv.SourceTimestamp)
        if dv.ServerTimestamp is None:
            # Absent means absent. It is NOT filled from source_ts, and not
            # from the receipt time: a server_ts equal to source_ts is exactly
            # the fingerprint T-07 exists to catch.
            server_ts = None
            self._counts.no_server_ts += 1
        else:
            server_ts = _as_utc(dv.ServerTimestamp)

        self.last_seen[tag["id"]] = time.monotonic()
        self._on_sample(Sample(
            tag_id=tag["id"], tag_name=name,
            source_ts=source_ts, server_ts=server_ts,
            value=value,
            quality=int(dv.StatusCode.value),
        ))


def _as_float(raw: object) -> tuple[float | None, bool]:
    """Coerce a DataValue's payload to the archive's double precision column.

    Returns (value, storable). None stays None and is storable: a Bad DataValue
    carries no value, and inventing one would be the substitution §318 forbids.
    Booleans become 1.0/0.0 because digitals are stored in the same column as
    analogues. Anything else -- a string, an array -- is not storable as a
    double, and is refused rather than coerced into a number that would look
    like a measurement.
    """
    if raw is None:
        return None, True
    if isinstance(raw, bool):            # checked before int: bool IS an int
        return (1.0 if raw else 0.0), True
    if isinstance(raw, (int, float)):
        return float(raw), True
    return None, False


def _as_utc(value: dt.datetime) -> dt.datetime:
    """asyncua hands back naive UTC datetimes. Attach the zone; never shift."""
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


class ReadOnlySession:
    """A subscribing, read-only OPC UA client."""

    NAMESPACE = "urn:orianode:crpms:sim"

    def __init__(self, endpoint: str, on_sample: Callable[[Sample], None], *,
                 source_deadband: bool | None = None,
                 on_publish_gap: Callable[[int, int, int], None] | None = None,
                 on_drop: Callable[[str, str], None] | None = None) -> None:
        self.endpoint = endpoint
        self._on_sample = on_sample
        # None: decided per server at connect, from config/sources.json. A bool
        # overrides that, for measuring the difference (deadband_evidence.py).
        self._deadband_override = source_deadband
        self.source_deadband: bool | None = None
        self.server_uri: str | None = None
        self._on_publish_gap = on_publish_gap
        self._on_drop = on_drop
        self._client: Client | None = None
        self._subscriptions: list = []
        self.subscribed: list[str] = []
        self.unresolved: list[str] = []
        self._handler: _SubscriptionHandler | None = None
        self._tag_by_node: dict = {}
        self.heartbeat_reads = 0
        self.counts = IngressCounts()
        self.sequence = PublishSequence()

    async def connect(self, tags: list[dict]) -> None:
        self._client = Client(url=self.endpoint)
        await self._client.connect()
        # Which server this is decides how it is treated. Its ApplicationUri is
        # the first entry of Server.ServerArray (Part 5). A read.
        server_array = await self._client.get_node(
            ua.ObjectIds.Server_ServerArray).read_value()
        self.server_uri = server_array[0] if server_array else None
        settings = source_settings(self.server_uri or "")
        self.source_deadband = (settings["source_deadband"]
                                if self._deadband_override is None
                                else self._deadband_override)
        log.info("source %s (%s): deadband at the source %s",
                 self.server_uri, settings["matched"] or "not listed, defaults",
                 "ON" if self.source_deadband else "OFF")
        idx = await self._client.get_namespace_index(self.NAMESPACE)
        objects = self._client.nodes.objects

        tag_by_node: dict = {}
        by_group: dict[tuple[float, float | None], list] = {}
        unresolved: list[str] = []

        for tag in tags:
            # The parent object is configuration and has no default. It was the
            # literal "Unit1" here, then a column default that did the same
            # thing quietly: Unit 2's tags were filed under Unit1. A tag with
            # no source path is refused, loudly, and counted -- one bad row must
            # not stop acquisition of every other tag, and it must not be
            # guessed at either.
            parent = tag.get("source_path")
            if not parent:
                log.error("tag %s has no source_path; it cannot be located in "
                          "the source address space and is NOT acquired",
                          tag["name"])
                unresolved.append(tag["name"])
                continue
            try:
                node = await objects.get_child(
                    [f"{idx}:{parent}", f"{idx}:{tag['name']}"])
            except ua.UaError:
                log.error("tag %s is not at %s in the server address space and "
                          "is NOT acquired", tag["name"], parent)
                unresolved.append(tag["name"])
                continue
            tag_by_node[node] = tag
            deadband = tag["exc_dev"] if self.source_deadband else None
            key = (float(tag["scan_rate_ms"] or 1000), deadband)
            by_group.setdefault(key, []).append(node)

        self.unresolved = unresolved
        self.counts.unresolved_tags = len(unresolved)
        handler = _SubscriptionHandler(self._on_sample, tag_by_node, self.counts,
                                       self._on_drop)
        self._handler = handler
        self._tag_by_node = tag_by_node

        # One subscription per (sampling interval, deadband) group, so each tag
        # is sampled at its configured rate rather than everything at the
        # fastest rate any tag needs (§317).
        #
        # SOURCE DEADBAND is a per-source setting (config/sources.json), on by
        # default, because at 34,700 I/O the deadband applied AT THE SOURCE is
        # what keeps unchanged values off the wire.
        #
        # It is OFF for the asyncua simulator, deliberately. asyncua 2.0.1's
        # server ANDs the data-change trigger with the deadband test
        # (monitored_item_service.py, `_is_data_changed(...) and
        # _is_deadband_exceeded(...)`). A change of StatusCode with the value
        # unchanged passes the trigger and fails the deadband, so it is never
        # sent. Measured against this simulator (collector/deadband_evidence.py,
        # recorded in fat/records/stage-06-quality-rules.md): with a source
        # deadband, forcing a tag to BadDeviceFailure produced zero Bad
        # notifications; without one, it produced the notification immediately.
        # Per OPC UA Part 4 a StatusValue trigger reports a status change
        # regardless of deadband, and a server that honours it can be trusted
        # with the deadband; this one cannot.
        for (interval_ms, deadband), nodes in by_group.items():
            subscription = await self._client.create_subscription(interval_ms, handler)
            self._watch_sequence(subscription)
            for node in nodes:
                if deadband:
                    # Trigger StatusValue, absolute deadband = ExcDev.
                    await subscription.deadband_monitor(node, deadband, queuesize=10)
                else:
                    await subscription.subscribe_data_change(node, queuesize=10)
            self._subscriptions.append(subscription)
            log.info("subscribed %d tags at %.0f ms, source deadband %s",
                     len(nodes), interval_ms,
                     deadband if deadband else "off")

        self.subscribed = [t["name"] for t in tag_by_node.values()]

    def _watch_sequence(self, subscription) -> None:
        """Check the server's NotificationMessage numbering on every publish
        response for this subscription, keep-alives included.

        asyncua registers `subscription.publish_callback` with the session when
        the subscription is created, and re-reads the attribute whenever it
        re-registers (transfer, recreate). So the wrapper is installed on the
        instance and the current registration is replaced with it. If a future
        asyncua moves the registry, this fails loudly at connect rather than
        silently checking nothing.
        """
        original = subscription.publish_callback
        registry = subscription.server._subscription_callbacks
        sub_id = subscription.subscription_id

        async def checked(publish_result: ua.PublishResult) -> None:
            message = publish_result.NotificationMessage
            keep_alive = not message.NotificationData
            missed = self.sequence.observe(publish_result.SubscriptionId,
                                           int(message.SequenceNumber), keep_alive)
            if missed:
                self.counts.publish_gaps += 1
                self.counts.publish_missed += missed
                log.error("subscription %s: %d NotificationMessage(s) missed "
                          "before sequence number %d", publish_result.SubscriptionId,
                          missed, message.SequenceNumber)
                if self._on_publish_gap is not None:
                    self._on_publish_gap(publish_result.SubscriptionId,
                                         int(message.SequenceNumber), missed)
            await original(publish_result)

        subscription.publish_callback = checked
        if sub_id not in registry:
            raise RuntimeError(
                f"asyncua did not register subscription {sub_id} where expected; "
                "publish sequence checking cannot be installed")
        registry[sub_id] = checked

    async def max_time_read(self, interval_s: float = 5.0) -> None:
        """Periodically read tags that have gone quiet, at their max_time.

        WHY THIS EXISTS. An OPC UA subscription reports on change. A transmitter
        that is stuck therefore produces SILENCE, not a stream of identical
        values -- so with subscription alone, "frozen" and "stale" are the same
        observable, and §439 requires them distinguished. A tag that is alive
        but stuck is a different fault from one whose link has failed, and they
        call for different responses.

        So a tag that has not been heard from for its max_time is READ. This is
        an ordinary OPC UA read, and the DataValue it returns carries the
        server's own SourceTimestamp -- the instant the simulator computed that
        value. Nothing is re-stamped and no measurement time is invented: the
        alternative, re-recording the last known value under a fresh timestamp,
        would be exactly the fabrication §335 forbids. A read that returns a
        SourceTimestamp already seen is the same sample again, and the pipeline
        absorbs it.

        Reads, never writes. The session remains read-only.
        """
        while True:
            await asyncio.sleep(interval_s)
            if self._client is None or self._handler is None:
                continue
            now = time.monotonic()
            for node, tag in list(self._tag_by_node.items()):
                max_time = (tag.get("max_time_ms") or 60000) / 1000.0
                last = self._handler.last_seen.get(tag["id"])
                if last is not None and now - last < max_time:
                    continue
                try:
                    dv = await node.read_data_value(raise_on_bad_status=False)
                except Exception as exc:
                    log.warning("max-time read of %s failed: %s", tag["name"], exc)
                    continue
                self._handler.handle(node, dv)
                self.heartbeat_reads += 1

    async def disconnect(self) -> None:
        for subscription in self._subscriptions:
            if subscription.subscription_id is not None:
                self.sequence.forget(subscription.subscription_id)
            try:
                await subscription.delete()
            except Exception:
                pass
        self._subscriptions.clear()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None
