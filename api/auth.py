"""Authentication and authorisation for the API. (§509)

Every route requires a principal. The principal comes from a bearer token,
resolved by `ops.access.authenticate` against the hashed tokens in `principal`.
What each route needs is DATA -- the table below, one line per route -- rather
than decorators scattered through the file, so the whole access policy can be
read in one place and a test can check that no route was left out.

A route that is not in the table is refused, not allowed: a new endpoint that
nobody thought about must fail closed.

The WebSocket carries the token in its subprotocol list -- a browser cannot set
an Authorization header on a WebSocket, and a token in a query string ends up in
access logs and browser history. The client offers ["crpms.bearer", <token>];
the server accepts "crpms.bearer" and never echoes the token back.

STATION SCOPE. The station role reads one station. Everything that can be tied
to a station is filtered or refused by it: a tag belongs to the station of the
element it is mapped to, and an element carries its station in its context. A
tag mapped to no element belongs to no station, and a station principal cannot
see it. What the scope does not cover is said here: /api/status reports the
collectors, which serve every station, and is readable by any principal with
read:data.
"""

from __future__ import annotations

import asyncio
import os

import psycopg
from fastapi import HTTPException, Request, WebSocket

from ops import access

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")

SUBPROTOCOL = "crpms.bearer"

# route path (as FastAPI declares it) -> the permission it needs.
ROUTE_PERMISSIONS: dict[str, str] = {
    "/api/whoami":                         "read:data",
    "/api/status":                         "read:data",
    "/api/tags":                           "read:config",
    "/api/trend/{tag_name}":               "read:data",
    "/api/assets":                         "read:config",
    "/api/assets/{asset_code}/attributes": "read:config",
    "/api/kpis":                           "read:data",
    "/api/events":                         "read:data",
    "/api/health/tags":                    "read:data",
    "/api/replay":                         "read:data",
    "/ws/events":                          "read:data",
}


def _authenticate_sync(token: str) -> access.Principal:
    with psycopg.connect(DSN, connect_timeout=5) as conn:
        return access.authenticate(conn, token)


async def _resolve(token: str | None) -> access.Principal:
    if not token:
        raise HTTPException(401, "a bearer token is required",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        return await asyncio.to_thread(_authenticate_sync, token)
    except access.AccessError as exc:
        raise HTTPException(401, str(exc),
                            headers={"WWW-Authenticate": "Bearer"}) from exc


def _require(principal: access.Principal, path: str) -> None:
    permission = ROUTE_PERMISSIONS.get(path)
    if permission is None:
        # Fail closed: an endpoint nobody declared a permission for.
        raise HTTPException(403, f"no permission is declared for {path}")
    if not principal.may(permission):
        raise HTTPException(
            403, f"{principal.username} has role {principal.role}, which does "
                 f"not carry {permission}")


async def principal(request: Request) -> access.Principal:
    """The FastAPI dependency every HTTP route takes."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    who = await _resolve(token.strip() if scheme.lower() == "bearer" else None)
    route = request.scope.get("route")
    _require(who, getattr(route, "path", request.url.path))
    return who


async def websocket_principal(socket: WebSocket) -> access.Principal | None:
    """Authenticate a WebSocket before accepting it. Returns None, having
    closed the socket with a policy-violation code, if it is refused."""
    offered = [p.strip() for p in
               socket.headers.get("sec-websocket-protocol", "").split(",")]
    token = None
    if SUBPROTOCOL in offered:
        index = offered.index(SUBPROTOCOL)
        if index + 1 < len(offered):
            token = offered[index + 1]
    try:
        who = await _resolve(token)
        _require(who, "/ws/events")
    except HTTPException as exc:
        # 1008: policy violation. Closed before accept, so nothing is sent.
        await socket.close(code=1008, reason=str(exc.detail)[:120])
        return None
    return who


# -- station scope ------------------------------------------------------------

async def tag_stations(names: list[str] | None = None) -> dict[str, str | None]:
    """Tag name -> the station of the element it is mapped to (None if none)."""
    sql = ("SELECT t.name, e.context->>'station' FROM tag t"
           " LEFT JOIN element e ON e.id = t.element_id")
    params: tuple = ()
    if names is not None:
        sql += " WHERE t.name = ANY(%s)"
        params = (names,)
    async with await psycopg.AsyncConnection.connect(DSN, connect_timeout=5) as c:
        async with c.cursor() as cur:
            await cur.execute(sql, params)
            return {name: station for name, station in await cur.fetchall()}


async def element_station(asset_code: str) -> str | None:
    async with await psycopg.AsyncConnection.connect(DSN, connect_timeout=5) as c:
        async with c.cursor() as cur:
            await cur.execute("SELECT context->>'station' FROM element"
                              " WHERE asset_code = %s", (asset_code,))
            row = await cur.fetchone()
            return row[0] if row else None


def refuse_station(who: access.Principal, station: str | None, what: str) -> None:
    if not who.may_see_station(station):
        raise HTTPException(
            403, f"{who.username} is scoped to station {who.station}; {what} is "
                 f"{'not mapped to any station' if station is None else 'at ' + station}")
