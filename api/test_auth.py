"""§509 against the API itself, not only the access library.

Runs a real API server on a spare port (or, with CRPMS_API_URL set, tests an
API that is already running -- which is how the FAT uses it) and makes real
HTTP and WebSocket requests with real tokens:

  no token                          -> 401 on every route
  an unknown token                  -> 401
  an admin token                    -> 200
  a station token, its own station  -> 200
  a station token, another station  -> 403, and lists filtered to nothing
  a station token, unmapped data    -> 403 (scope fails closed)
  a route with no declared permission -> refused
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import psycopg
import pytest

from api import auth
from ops import access

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
PREFIX = "test.api."
ROUTES = ["/api/whoami", "/api/status", "/api/tags", "/api/trend/U1_MW",
          "/api/assets", "/api/assets/KPCL-RTPS-U1/attributes", "/api/kpis",
          "/api/events", "/api/health/tags",
          "/api/replay?start=2026-09-23T00:00:00%2B00:00"
          "&end=2026-09-23T00:01:00%2B00:00&tags=U1_MW"]


@pytest.fixture(scope="module")
def base_url():
    external = os.environ.get("CRPMS_API_URL")
    if external:
        yield external.rstrip("/")
        return
    import uvicorn
    from api.main import app
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def tokens():
    try:
        conn = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"archive not reachable: {exc}")
    made = {
        "admin": access.create_principal(conn, PREFIX + "admin", "admin",
                                         actor="test"),
        "rtps": access.create_principal(conn, PREFIX + "rtps", "station",
                                        station="RTPS", actor="test"),
        "btps": access.create_principal(conn, PREFIX + "btps", "station",
                                        station="BTPS", actor="test"),
    }
    yield made
    # The principals are test data and go; their audit rows are true records
    # of what the test did and stay.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM principal WHERE username LIKE %s", (PREFIX + "%",))
    conn.commit()
    conn.close()


def get(base: str, path: str, token: str | None = None):
    request = urllib.request.Request(base + path)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_every_route_declares_its_permission():
    from api.main import app
    declared = set(auth.ROUTE_PERMISSIONS)
    served = {r.path for r in app.routes
              if r.path.startswith(("/api/", "/ws/"))}
    assert served == declared


@pytest.mark.parametrize("path", ROUTES)
def test_no_token_is_401(base_url, tokens, path):
    status, _ = get(base_url, path)
    assert status == 401


def test_an_unknown_token_is_401(base_url, tokens):
    status, _ = get(base_url, "/api/tags", "not-a-real-token")
    assert status == 401


@pytest.mark.parametrize("path", ROUTES)
def test_admin_reads_every_route(base_url, tokens, path):
    status, _ = get(base_url, path, tokens["admin"])
    assert status == 200


def test_a_station_reads_its_own_station(base_url, tokens):
    status, body = get(base_url, "/api/trend/U1_MW?minutes=1", tokens["rtps"])
    assert status == 200 and body["tag"] == "U1_MW"
    status, tags = get(base_url, "/api/tags", tokens["rtps"])
    assert status == 200 and tags
    assert {t["station"] for t in tags} == {"RTPS"}


def test_a_station_cannot_read_another_station(base_url, tokens):
    status, body = get(base_url, "/api/trend/U1_MW", tokens["btps"])
    assert status == 403
    assert "BTPS" in body["detail"] and "RTPS" in body["detail"]
    status, _ = get(base_url, "/api/assets/KPCL-RTPS-U1/attributes", tokens["btps"])
    assert status == 403
    status, _ = get(base_url, ROUTES[-1], tokens["btps"])
    assert status == 403
    for path in ("/api/tags", "/api/assets", "/api/kpis", "/api/events",
                 "/api/health/tags"):
        status, rows = get(base_url, path, tokens["btps"])
        assert status == 200 and rows == [], path


def test_a_station_cannot_read_data_that_belongs_to_no_station(base_url, tokens):
    status, _ = get(base_url, "/api/trend/COLLECTOR_PRIMARY_LINK_STATE",
                    tokens["rtps"])
    assert status == 403
    status, _ = get(base_url, "/api/trend/COLLECTOR_PRIMARY_LINK_STATE",
                    tokens["admin"])
    assert status == 200


def test_whoami_names_the_principal(base_url, tokens):
    status, body = get(base_url, "/api/whoami", tokens["rtps"])
    assert status == 200
    assert (body["username"], body["role"], body["station"]) == (
        PREFIX + "rtps", "station", "RTPS")


def test_an_undeclared_route_fails_closed():
    who = access.Principal("x", "admin", None, frozenset({"read:data"}))
    with pytest.raises(Exception) as caught:
        auth._require(who, "/api/something-new")
    assert caught.value.status_code == 403


def test_a_missing_permission_is_403():
    who = access.Principal("x", "operations", None, frozenset({"read:data"}))
    with pytest.raises(Exception) as caught:
        auth._require(who, "/api/tags")          # needs read:config
    assert caught.value.status_code == 403


def test_the_websocket_refuses_without_a_token_and_accepts_with_one(base_url, tokens):
    import websockets
    url = base_url.replace("http", "ws", 1) + "/ws/events"

    async def attempt(subprotocols):
        try:
            async with websockets.connect(url, subprotocols=subprotocols,
                                          open_timeout=5) as ws:
                return ws.subprotocol
        except Exception as exc:                      # noqa: BLE001
            return exc

    refused = asyncio.run(attempt(None))
    assert isinstance(refused, Exception)
    wrong = asyncio.run(attempt([auth.SUBPROTOCOL, "not-a-real-token"]))
    assert isinstance(wrong, Exception)
    accepted = asyncio.run(attempt([auth.SUBPROTOCOL, tokens["admin"]]))
    assert accepted == auth.SUBPROTOCOL
