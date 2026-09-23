"""FastAPI service: the collector's event stream, and the archive, for the UI.

The WebSocket relays the collector's own event stream (collector/event_server.py),
which deliberately does not pass through the archive. The REST endpoints read
the archive.

EVERY ROUTE IS AUTHENTICATED AND AUTHORISED (§509). Each takes the `principal`
dependency from api/auth.py, which resolves a bearer token and checks the
route's permission from one table. The station role is scoped to its station on
every route that returns station data.

ONE RULE RUNS THROUGH EVERY ENDPOINT. Quality is returned with every value, and
a value that is not Good is returned as null with its StatusCode and its reason
— never as zero, and never omitted so that a chart can quietly close the gap.
A trend that cannot show that a reading was Bad is worse than no trend.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import (Depends, FastAPI, HTTPException, Query, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware

from api import auth
from ops.access import Principal

log = logging.getLogger("api")

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
# The collector serves its own event stream. This deliberately does NOT go
# through the database: the pipeline view exists to show what happens when the
# archive is unreachable, and routing the events through the archive would
# freeze the picture at the exact moment it has something to say.
COLLECTOR_EVENTS = os.environ.get("CRPMS_COLLECTOR_EVENTS",
                                  "ws://127.0.0.1:8090/events")


class Hub:
    """Fans live events out to connected browsers, each seeing only what its
    principal may see."""

    def __init__(self) -> None:
        self.clients: dict[WebSocket, Principal] = {}
        self.received = 0
        self.recent: list[dict] = []
        self.stations: dict[str, str | None] = {}

    async def refresh_stations(self) -> None:
        try:
            self.stations = await auth.tag_stations()
        except psycopg.Error:
            # Keep the last map: the archive being down is exactly when the
            # event stream has something to show.
            pass

    def visible(self, who: Principal, event: dict) -> bool:
        """An event about a tag is shown only to a principal who may see that
        tag's station. Events about the pipeline itself carry no tag."""
        tag = event.get("tag")
        return tag is None or who.may_see_station(self.stations.get(tag))

    async def publish(self, event: dict) -> None:
        self.received += 1
        self.recent.append(event)
        del self.recent[:-500]
        dead = []
        for client, who in list(self.clients.items()):
            if not self.visible(who, event):
                continue
            try:
                await client.send_json(event)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.pop(client, None)


hub = Hub()


async def _listen() -> None:
    """Subscribe to the collector's event stream and fan it out to browsers."""
    import websockets
    await hub.refresh_stations()
    while True:
        try:
            async with websockets.connect(COLLECTOR_EVENTS,
                                          ping_interval=20) as socket:
                log.info("subscribed to %s", COLLECTOR_EVENTS)
                async for message in socket:
                    try:
                        await hub.publish(json.loads(message))
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            log.warning("collector event stream unavailable: %s", exc)
            await asyncio.sleep(2)


async def _refresh_stations() -> None:
    while True:
        await asyncio.sleep(60)
        await hub.refresh_stations()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_listen()),
             asyncio.create_task(_refresh_stations())]
    yield
    for task in tasks:
        task.cancel()


# The generated schema and docs pages carry no data, but they are unauthenticated,
# so they are off unless asked for.
_DOCS = os.environ.get("CRPMS_API_DOCS") == "1"
app = FastAPI(title="CRPMS API", version="1.0", lifespan=lifespan,
              description="Event stream and archive for the CRPMS visualisation.",
              docs_url="/docs" if _DOCS else None,
              redoc_url="/redoc" if _DOCS else None,
              openapi_url="/openapi.json" if _DOCS else None)
# Only the visualisation's own origins, and only what it uses. The UI is served
# by Vite and proxies /api and /ws to this service, so in normal use no request
# is cross-origin at all; this is for a UI served from elsewhere.
UI_ORIGINS = [o.strip() for o in os.environ.get(
    "CRPMS_UI_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(",")
    if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=UI_ORIGINS,
                   allow_methods=["GET"],
                   allow_headers=["Authorization", "Content-Type"])


async def fetch(sql: str, params: tuple = ()) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(DSN, connect_timeout=5) as c:
        async with c.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


# -- live --------------------------------------------------------------------

@app.websocket("/ws/events")
async def ws_events(socket: WebSocket) -> None:
    who = await auth.websocket_principal(socket)
    if who is None:
        return
    await socket.accept(subprotocol=auth.SUBPROTOCOL)
    hub.clients[socket] = who
    # Replay what just happened, so a browser opened mid-outage sees the state
    # rather than an empty canvas until the next event.
    for event in hub.recent[-100:]:
        if hub.visible(who, event):
            await socket.send_json(event)
    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.pop(socket, None)


@app.get("/api/whoami")
async def whoami(who: Principal = Depends(auth.principal)) -> dict:
    return {"username": who.username, "role": who.role, "station": who.station,
            "permissions": sorted(who.permissions)}


@app.get("/api/status")
async def status(who: Principal = Depends(auth.principal)) -> dict:
    rows = await fetch(
        "SELECT instance, alive, is_leader, samples, link_up, buffer_depth,"
        " EXTRACT(epoch FROM age)::float FROM collector_leader")
    return {
        "collectors": [
            {"instance": r[0], "alive": r[1], "is_leader": r[2],
             "samples": r[3], "link_up": r[4], "buffer_depth": r[5],
             "age_s": round(r[6], 1)} for r in rows],
        "websocket_clients": len(hub.clients),
        "events_received": hub.received,
    }


# -- tags and trends ----------------------------------------------------------

@app.get("/api/tags")
async def tags(who: Principal = Depends(auth.principal)) -> list[dict]:
    rows = await fetch(
        "SELECT t.id, t.name, t.description, t.engineering_unit, t.range_low,"
        " t.range_high, t.source_system, e.asset_code, e.context->>'station'"
        " FROM tag t LEFT JOIN element e ON e.id = t.element_id ORDER BY t.name")
    return [{"id": r[0], "name": r[1], "description": r[2], "unit": r[3],
             "range_low": r[4], "range_high": r[5], "source_system": r[6],
             "asset_code": r[7], "station": r[8]}
            for r in rows if who.may_see_station(r[8])]


@app.get("/api/trend/{tag_name}")
async def trend(tag_name: str, minutes: float = Query(10, gt=0, le=1440),
                limit: int = Query(5000, gt=0, le=50000),
                who: Principal = Depends(auth.principal)) -> dict:
    """A trend, with quality on every point.

    A point that is not Good is returned with `value: null` and its StatusCode.
    It is NOT omitted: a chart that silently joins across a Bad reading draws a
    line through data that never existed, which is precisely the smoothing this
    project refuses to do. The consumer is told, and can break the line.

    `server_ts` and `transit_ms` are null for a sample no server stamped -- the
    collector's own health, or a DataValue that arrived without one.
    """
    stations = await auth.tag_stations([tag_name])
    if tag_name not in stations:
        raise HTTPException(404, f"no such tag: {tag_name}")
    auth.refuse_station(who, stations[tag_name], f"tag {tag_name}")
    rows = await fetch(
        "SELECT s.source_ts, s.server_ts, s.value, s.quality,"
        "       quality_class(s.quality)"
        " FROM sample s JOIN tag t ON t.id = s.tag_id"
        " WHERE t.name = %s AND s.source_ts > now() - (%s || ' minutes')::interval"
        " ORDER BY s.source_ts DESC LIMIT %s", (tag_name, str(minutes), limit))
    points = [{"source_ts": r[0].isoformat(),
               "server_ts": r[1].isoformat() if r[1] else None,
               "value": r[2], "quality": r[3], "quality_class": r[4],
               "transit_ms": (round((r[1] - r[0]).total_seconds() * 1000, 1)
                              if r[1] else None)}
              for r in reversed(rows)]
    return {"tag": tag_name, "points": points, "count": len(points)}


# -- asset hierarchy -----------------------------------------------------------

@app.get("/api/assets")
async def assets(who: Principal = Depends(auth.principal)) -> list[dict]:
    rows = await fetch(
        "SELECT e.id, e.asset_code, e.name, e.level, e.parent_id, t.name,"
        " e.context->>'station'"
        " FROM element e LEFT JOIN element_template t ON t.id = e.template_id"
        " ORDER BY e.asset_code")
    return [{"id": r[0], "asset_code": r[1], "name": r[2], "level": r[3],
             "parent_id": r[4], "template": r[5], "station": r[6]}
            for r in rows if who.may_see_station(r[6])]


@app.get("/api/assets/{asset_code}/attributes")
async def attributes(asset_code: str,
                     who: Principal = Depends(auth.principal)) -> list[dict]:
    auth.refuse_station(who, await auth.element_station(asset_code),
                        f"element {asset_code}")
    rows = await fetch(
        "SELECT a.name, t.name, t.engineering_unit, a.static_value"
        " FROM element e JOIN attribute a ON a.element_id = e.id"
        " LEFT JOIN tag t ON t.id = a.tag_id"
        " WHERE e.asset_code = %s ORDER BY a.name", (asset_code,))
    if not rows:
        raise HTTPException(404, f"no such element: {asset_code}")
    return [{"attribute": r[0], "tag": r[1], "unit": r[2], "static_value": r[3]}
            for r in rows]


# -- KPIs ----------------------------------------------------------------------

@app.get("/api/kpis")
async def kpis(asset_code: str | None = None,
               who: Principal = Depends(auth.principal)) -> list[dict]:
    """Latest KPI value per definition and element, with quality and reason.

    A Bad KPI returns `value: null` with the reason naming the offending input.
    That reason is the whole point of the endpoint: a dashboard that shows a
    blank cell teaches nothing, one that shows "coal flow is BadDeviceFailure"
    tells an engineer where to go.
    """
    sql = ("SELECT DISTINCT ON (d.name, e.asset_code)"
           " d.name, d.classification, k.kpi_version, e.asset_code, k.ts,"
           " k.value, k.quality, quality_class(k.quality), k.reason,"
           " d.engineering_unit, d.reference_value, d.validity_low, d.validity_high,"
           " e.context->>'station'"
           " FROM kpi_value k"
           " JOIN kpi_definition d ON d.id = k.kpi_definition_id"
           " JOIN element e ON e.id = k.element_id")
    params: tuple = ()
    if asset_code:
        sql += " WHERE e.asset_code = %s"
        params = (asset_code,)
    sql += " ORDER BY d.name, e.asset_code, k.ts DESC"
    rows = await fetch(sql, params)
    return [{"kpi": r[0], "classification": r[1], "version": r[2],
             "asset_code": r[3], "ts": r[4].isoformat(), "value": r[5],
             "quality": r[6], "quality_class": r[7], "reason": r[8],
             "unit": r[9], "reference": r[10],
             "validity_low": r[11], "validity_high": r[12]}
            for r in rows if who.may_see_station(r[13])]


# -- event frames ---------------------------------------------------------------

@app.get("/api/events")
async def event_frames(asset_code: str | None = None,
                       limit: int = Query(20, gt=0, le=200),
                       who: Principal = Depends(auth.principal)) -> list[dict]:
    sql = ("SELECT f.id, f.template, e.asset_code, f.start_ts, f.end_ts,"
           " f.status, f.is_reference FROM event_frame f"
           " JOIN element e ON e.id = f.element_id")
    conditions: list[str] = []
    params: list = []
    if asset_code:
        conditions.append("e.asset_code = %s")
        params.append(asset_code)
    if who.role == "station":
        # Filtered in the query, so LIMIT counts frames the principal may see.
        conditions.append("e.context->>'station' = %s")
        params.append(who.station)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY f.start_ts DESC LIMIT %s"
    params.append(limit)
    frames = await fetch(sql, tuple(params))
    out = []
    for fid, template, code, start, end, status, is_ref in frames:
        milestones = await fetch(
            "SELECT name, ts, value, quality FROM event_milestone"
            " WHERE event_frame_id = %s ORDER BY sort_order, ts", (fid,))
        out.append({
            "id": fid, "template": template, "asset_code": code,
            "start_ts": start.isoformat(),
            "end_ts": end.isoformat() if end else None,
            "status": status, "is_reference": is_ref,
            "duration_s": (end - start).total_seconds() if end else None,
            "milestones": [
                {"name": m[0], "ts": m[1].isoformat(),
                 "offset_s": (m[1] - start).total_seconds(),
                 "value": m[2], "quality": m[3]} for m in milestones],
        })
    return out


# -- tag health -----------------------------------------------------------------

@app.get("/api/health/tags")
async def tag_health(who: Principal = Depends(auth.principal)) -> list[dict]:
    rows = await fetch(
        "SELECT tag_name, worst_state, source_quality, source_class,"
        " computed_quality, computed_class, last_source_ts, detail"
        " FROM tag_health_decoded ORDER BY tag_name")
    stations = await auth.tag_stations([r[0] for r in rows])
    return [{"tag": r[0], "state": r[1], "source_quality": r[2],
             "source_class": r[3], "computed_quality": r[4],
             "computed_class": r[5],
             "last_source_ts": r[6].isoformat() if r[6] else None,
             "detail": r[7]} for r in rows
            if who.may_see_station(stations.get(r[0]))]


# -- replay ----------------------------------------------------------------------

@app.get("/api/replay")
async def replay(start: str, end: str,
                 tags: str = Query(..., description="comma-separated tag names"),
                 who: Principal = Depends(auth.principal)) -> dict:
    """Everything needed to replay a window: samples with quality, and the
    events that were emitted during it.

    This is what the time scrubber reads. It replays what the archive holds,
    which is what actually happened — not a recording of the animation.
    """
    try:
        since = dt.datetime.fromisoformat(start)
        until = dt.datetime.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(400, f"bad timestamp: {exc}") from exc
    names = [t.strip() for t in tags.split(",") if t.strip()]
    stations = await auth.tag_stations(names)
    for name in names:
        if name not in stations:
            raise HTTPException(404, f"no such tag: {name}")
        auth.refuse_station(who, stations[name], f"tag {name}")
    rows = await fetch(
        "SELECT t.name, s.source_ts, s.server_ts, s.value, s.quality,"
        " quality_class(s.quality) FROM sample s JOIN tag t ON t.id = s.tag_id"
        " WHERE t.name = ANY(%s) AND s.source_ts BETWEEN %s AND %s"
        " ORDER BY s.source_ts", (names, since, until))
    series: dict[str, list] = {n: [] for n in names}
    for name, source_ts, server_ts, value, quality, klass in rows:
        series[name].append({
            "source_ts": source_ts.isoformat(),
            "server_ts": server_ts.isoformat() if server_ts else None,
            "value": value,
            "quality": quality, "quality_class": klass})
    return {"start": since.isoformat(), "end": until.isoformat(),
            "series": series,
            "counts": {k: len(v) for k, v in series.items()}}


def main() -> int:
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(prog="api")
    ap.add_argument("--host", default=os.environ.get("API_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("API_PORT", "8000")))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    logging.Formatter.converter = __import__("time").gmtime   # UTC, like the data
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
