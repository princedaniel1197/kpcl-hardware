"""The collector's own event stream, served directly over WebSocket.

WHY THIS EXISTS INSTEAD OF THE OBVIOUS THING. The first design relayed events
through Postgres LISTEN/NOTIFY — elegant, no new listener, uses infrastructure
that is already required. It was also wrong: the whole point of the pipeline
view is to show what happens when the ARCHIVE becomes unreachable, and routing
the events through the archive means the picture freezes at exactly the moment
it has something to say. The buffer would fill, the collector would keep
working perfectly, and the screen would show nothing at all.

So the stream comes straight from the collector, over a socket that does not
touch the database. An archive outage is then visible rather than invisible,
which is the entire reason the view exists.

This serves events. It accepts nothing: the socket is send-only, and the
collector still has no write path of any kind.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger("collector.event_server")


def build_app(stream, pipeline, instance: str) -> FastAPI:
    app = FastAPI(title="CRPMS collector event stream", version="1.0")

    @app.websocket("/events")
    async def events(socket: WebSocket) -> None:
        await socket.accept()
        subscription = stream.subscribe()
        try:
            async for event in subscription:
                event["instance"] = instance
                await socket.send_text(json.dumps(event, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await subscription.aclose()

    @app.get("/health")
    async def health() -> dict:
        return {
            "instance": instance,
            "received": pipeline.received,
            "forwarded": pipeline.forwarded,
            "buffered": pipeline.buffered,
            "drained": pipeline.drained,
            "gaps": pipeline.gaps,
            "link_up": pipeline.link_up,
            "draining": pipeline.draining,
            "buffer_depth": pipeline.buffer.depth,
            "queued": pipeline.queued,
            "events_emitted": stream.emitted,
            "events_dropped": stream.dropped,
        }

    return app


async def serve(stream, pipeline, instance: str, host: str, port: int) -> None:
    import uvicorn
    config = uvicorn.Config(build_app(stream, pipeline, instance), host=host,
                            port=port, log_level="warning", access_log=False)
    log.info("event stream on ws://%s:%d/events", host, port)
    await uvicorn.Server(config).serve()
