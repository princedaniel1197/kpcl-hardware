"""The collector's event stream.

Every meaningful occurrence is emitted as a small JSON-serialisable event:
value received, value refused, value buffered, buffer drained, publish gap
detected, quality changed, link state changed. Stage 12's visualisation consumes
it; nothing here knows what a browser is.

Subscribers that cannot keep up lose the oldest events rather than blocking
acquisition. A slow consumer must never be able to stall the collector.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, AsyncIterator

VALUE_RECEIVED = "value_received"
VALUE_REFUSED = "value_refused"
VALUE_BUFFERED = "value_buffered"
VALUE_FORWARDED = "value_forwarded"
BUFFER_DRAINED = "buffer_drained"
BUFFER_DRAIN_STARTED = "buffer_drain_started"
BUFFER_OVERFLOW = "buffer_overflow"
BUFFER_HIGH_WATER = "buffer_high_water"
GAP_DETECTED = "gap_detected"
QUALITY_CHANGED = "quality_changed"
LINK_STATE = "link_state"


class EventStream:
    """In-process fan-out to subscribers.

    The visualisation runs in a different process; `collector/event_server.py`
    serves this stream to it over a WebSocket of the collector's own, which
    does not touch the archive. (An earlier design relayed events through
    Postgres LISTEN/NOTIFY, and froze the picture at the moment the archive
    went away -- the moment the picture exists to show.) A dropped event is a
    dropped *picture*, never a dropped sample: the archive is the record, and
    this stream is only how the picture moves.
    """

    def __init__(self, queue_size: int = 2000) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._queue_size = queue_size
        self.dropped = 0
        self.emitted = 0

    def emit(self, kind: str, **fields: Any) -> dict:
        event = {
            "kind": kind,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            **fields,
        }
        self.emitted += 1
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest and take the newest: a subscriber that has
                # stopped reading must not stall acquisition.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
                self.dropped += 1
        return event

    async def subscribe(self) -> AsyncIterator[dict]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.remove(queue)
