"""The OPC UA session.

THIS SESSION IS READ-ONLY. There is no write method in this module, none in
this package, and none behind a flag or a comment. That is an architectural
guarantee rather than a promise, and `test_readonly.py` asserts it by
inspecting the package's own source. (§303, §315)

Subscription, not polling: each tag is subscribed with its own sampling
interval and deadband, taken from the tag table. The load placed on the source
is a configured quantity, not an assumption (§317).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Callable

from asyncua import Client, ua

from collector.model import Sample

log = logging.getLogger("collector.session")


class _SubscriptionHandler:
    """Receives data changes and turns them into Samples.

    asyncua calls `datachange_notification` with the raw DataValue, which is the
    only place the four quantities -- value, StatusCode, SourceTimestamp,
    ServerTimestamp -- are all available together. They are extracted here and
    travel onward as one object.
    """

    def __init__(self, on_sample: Callable[[Sample], None],
                 tag_by_node: dict) -> None:
        self._on_sample = on_sample
        self._tag_by_node = tag_by_node
        self._seq: dict[int, int] = {}
        # When each tag was last heard from, for the max-time read below.
        self.last_seen: dict[int, float] = {}

    def datachange_notification(self, node, value, data) -> None:
        self.handle(node, data.monitored_item.Value)

    def handle(self, node, dv) -> None:
        """Turn one DataValue into a Sample.

        Used by the subscription and by the periodic max-time read, so both
        paths extract the four quantities identically.
        """
        tag = self._tag_by_node.get(node)
        if tag is None:
            return

        # Never substitute receipt time for a valid SourceTimestamp (§335).
        # If the server sent none at all, the sample is unusable as a
        # measurement and is dropped with a loud log -- inventing one here
        # would be indistinguishable from the thing this project exists to
        # prevent.
        if dv.SourceTimestamp is None:
            log.error("%s: DataValue carried no SourceTimestamp; dropped",
                      tag["name"])
            return

        source_ts = _as_utc(dv.SourceTimestamp)
        server_ts = _as_utc(dv.ServerTimestamp) if dv.ServerTimestamp else source_ts

        # A Bad DataValue carries no value (OPC UA Part 4, measured in Stage 1).
        # dv.Value.Value is then None and stays None.
        raw = dv.Value.Value if dv.Value is not None else None

        seq = self._seq.get(tag["id"], 0) + 1
        self._seq[tag["id"]] = seq
        self.last_seen[tag["id"]] = time.monotonic()

        self._on_sample(Sample(
            tag_id=tag["id"], tag_name=tag["name"],
            source_ts=source_ts, server_ts=server_ts,
            value=_as_float(raw, tag["name"]),
            quality=int(dv.StatusCode.value) if dv.StatusCode is not None else 0,
            seq=seq,
        ))


def _as_float(raw: object, tag_name: str) -> float | None:
    """Coerce a DataValue's payload to the archive's double precision column.

    None stays None: a Bad DataValue carries no value, and inventing one would
    be the substitution §318 forbids. Booleans become 1.0/0.0 because digitals
    are stored in the same column as analogues. Anything else -- a string, an
    array -- is not storable as a double, so it is refused rather than coerced
    into a number that would look like a measurement.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):            # checked before int: bool IS an int
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    log.error("%s: value %r is not storable as a double; stored as no value",
              tag_name, raw)
    return None


def _as_utc(value: dt.datetime) -> dt.datetime:
    """asyncua hands back naive UTC datetimes. Attach the zone; never shift."""
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


class ReadOnlySession:
    """A subscribing, read-only OPC UA client."""

    NAMESPACE = "urn:orianode:crpms:sim"

    def __init__(self, endpoint: str, on_sample: Callable[[Sample], None]) -> None:
        self.endpoint = endpoint
        self._on_sample = on_sample
        self._client: Client | None = None
        self._subscriptions: list = []
        self.subscribed: list[str] = []
        self._handler: _SubscriptionHandler | None = None
        self._tag_by_node: dict = {}
        self.heartbeat_reads = 0

    async def connect(self, tags: list[dict]) -> None:
        self._client = Client(url=self.endpoint)
        await self._client.connect()
        idx = await self._client.get_namespace_index(self.NAMESPACE)
        objects = self._client.nodes.objects

        tag_by_node: dict = {}
        by_interval: dict[tuple[float, float | None], list] = {}

        for tag in tags:
            # The parent object is configuration. It used to be the literal
            # "Unit1", which meant adding any second unit needed a code change
            # -- exactly what rule 7 forbids.
            parent = tag.get("source_path") or "Unit1"
            try:
                node = await objects.get_child(
                    [f"{idx}:{parent}", f"{idx}:{tag['name']}"])
            except ua.UaError:
                log.warning("tag %s is not in the server address space", tag["name"])
                continue
            tag_by_node[node] = tag
            key = (float(tag["scan_rate_ms"] or 1000), tag["exc_dev"])
            by_interval.setdefault(key, []).append(node)

        handler = _SubscriptionHandler(self._on_sample, tag_by_node)
        self._handler = handler
        self._tag_by_node = tag_by_node

        # One subscription per sampling-interval group, so each tag is sampled
        # at its configured rate rather than everything at the fastest rate any
        # tag needs. The load placed on the source is configuration, not an
        # assumption (§317).
        #
        # NO SERVER-SIDE DEADBAND IS APPLIED, deliberately, and this is a
        # decision rather than an omission.
        #
        # asyncua 2.0.1's server ANDs the data-change trigger with the deadband
        # test (monitored_item_service.py, `_is_data_changed(...) and
        # _is_deadband_exceeded(...)`). A change of StatusCode with the value
        # unchanged passes the trigger and fails the deadband, so it is never
        # sent. Measured against this simulator: with a deadband, forcing a tag
        # to BadDeviceFailure produced zero Bad notifications; without one, it
        # produced the notification immediately.
        #
        # Per OPC UA Part 4 a deadband governs VALUE changes; a StatusValue
        # trigger should report a status change regardless. Whatever the
        # library ought to do, a filter that can hide a tag going Bad cannot sit
        # in the acquisition path of a system whose central claim is that
        # quality is never lost (§318, CLAUDE.md rule 2).
        #
        # The deadband's purpose -- not archiving values that carry no
        # information -- is served instead by Stage 4's two-stage compression,
        # which forces an archive on a quality change by construction. The cost
        # is more traffic from the source, which is measured rather than
        # assumed; see fat/records/stage-06-quality-rules.md.
        for (interval_ms, deadband), nodes in by_interval.items():
            subscription = await self._client.create_subscription(interval_ms, handler)
            for node in nodes:
                await subscription.subscribe_data_change(node, queuesize=10)
            self._subscriptions.append(subscription)
            log.info("subscribed %d tags at %.0f ms (deadband %s applied "
                     "downstream, not at the source)",
                     len(nodes), interval_ms, deadband if deadband else "none")

        self.subscribed = [t["name"] for t in tag_by_node.values()]

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
        would be exactly the fabrication §335 forbids.

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
