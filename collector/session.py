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

import datetime as dt
import logging
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

    def datachange_notification(self, node, value, data) -> None:
        tag = self._tag_by_node.get(node)
        if tag is None:
            return

        dv = data.monitored_item.Value

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

    async def connect(self, tags: list[dict]) -> None:
        self._client = Client(url=self.endpoint)
        await self._client.connect()
        idx = await self._client.get_namespace_index(self.NAMESPACE)
        objects = self._client.nodes.objects

        tag_by_node: dict = {}
        by_interval: dict[tuple[float, float | None], list] = {}

        for tag in tags:
            try:
                node = await objects.get_child([f"{idx}:Unit1", f"{idx}:{tag['name']}"])
            except ua.UaError:
                log.warning("tag %s is not in the server address space", tag["name"])
                continue
            tag_by_node[node] = tag
            key = (float(tag["scan_rate_ms"] or 1000), tag["exc_dev"])
            by_interval.setdefault(key, []).append(node)

        handler = _SubscriptionHandler(self._on_sample, tag_by_node)

        # One subscription per (interval, deadband) group, so each tag is
        # sampled at its configured rate rather than everything at the fastest
        # rate any tag needs. The load placed on the source is configuration,
        # not an assumption (§317).
        for (interval_ms, deadband), nodes in by_interval.items():
            subscription = await self._client.create_subscription(interval_ms, handler)
            if deadband:
                # asyncua 2.x has no monitoring_filter argument on
                # subscribe_data_change; deadband_monitor is the supported way.
                # It sets Trigger = StatusValue, which matters here: a quality
                # change must report even when the value sits inside the
                # deadband, or a tag going Bad while steady would be invisible.
                for node in nodes:
                    await subscription.deadband_monitor(
                        node, float(deadband), deadbandtype=1, queuesize=10)
            else:
                # No deadband: digitals, where every transition matters (§440).
                for node in nodes:
                    await subscription.subscribe_data_change(node, queuesize=10)
            self._subscriptions.append(subscription)
            log.info("subscribed %d tags at %.0f ms, deadband %s",
                     len(nodes), interval_ms,
                     deadband if deadband else "none (every change)")

        self.subscribed = [t["name"] for t in tag_by_node.values()]

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
