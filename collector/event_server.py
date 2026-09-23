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


def build_app(stream, pipeline, instance: str, session=None) -> FastAPI:
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
        counts = session.counts if session is not None else None
        return {
            "instance": instance,
            "run": pipeline.run_id,
            "received": pipeline.received,
            "forwarded": pipeline.forwarded,
            "buffered": pipeline.buffered,
            "drained": pipeline.drained,
            "duplicate_ts": pipeline.duplicate_ts,
            "compressed_out": pipeline.compressed_out,
            "compressed_tags": len(pipeline.compressors),
            "buffer_lost": pipeline.buffer.overflowed,
            "link_up": pipeline.link_up,
            "draining": pipeline.draining,
            "buffer_depth": pipeline.buffer.depth,
            "queued": pipeline.queued,
            "events_emitted": stream.emitted,
            "events_dropped": stream.dropped,
            "source": None if session is None else {
                "application_uri": session.server_uri,
                "source_deadband": session.source_deadband,
                "subscribed": len(session.subscribed),
                "unresolved": session.unresolved,
                "heartbeat_reads": session.heartbeat_reads,
                "publish_gaps": counts.publish_gaps,
                "publish_missed": counts.publish_missed,
                "dropped_no_source_ts": counts.dropped_no_source_ts,
                "dropped_no_status": counts.dropped_no_status,
                "dropped_unstorable": counts.dropped_unstorable,
                "no_server_ts": counts.no_server_ts,
            },
        }

    return app


async def serve(stream, pipeline, instance: str, host: str, port: int,
                session=None) -> None:
    import uvicorn
    config = uvicorn.Config(build_app(stream, pipeline, instance, session),
                            host=host,
                            port=port, log_level="warning", access_log=False)
    log.info("event stream on ws://%s:%d/events", host, port)
    await uvicorn.Server(config).serve()
