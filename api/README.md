# api — FastAPI and WebSocket (Stage 12)

REST endpoints for tags, trends, asset hierarchy, KPI values, event frames, tag
health and replay, plus a WebSocket relaying the collector's event stream to the
browser. FastAPI is the only web framework in this project.

```bash
make api       # uvicorn on 127.0.0.1:8000
make token     # a read-only token (role corporate), shown once
```

## Every route is authenticated and authorised (§509)

`api/auth.py`. A bearer token is resolved by `ops.access.authenticate` against
the SHA-256 hashes in `principal`; what each route needs is one table,
`ROUTE_PERMISSIONS`, and a route that is not in it is refused — a new endpoint
nobody thought about fails closed. A test checks the table covers every route.

The **station** role is scoped to its station on every route that returns
station data: a tag belongs to the station of the element it is mapped to, and
a tag mapped to none is refused to a station principal rather than assumed
visible. Lists are filtered; single items are refused with 403. `/api/status`
reports the collectors, which serve every station, and is not scoped.

The WebSocket takes the token as its second offered subprotocol
(`["crpms.bearer", <token>]`), because a browser cannot set a header on a
WebSocket and a token in a URL ends up in logs and history. Events about a tag
are sent only to principals who may see that tag's station.

CORS allows only the UI's origins (`CRPMS_UI_ORIGINS`) and GET. The generated
schema and docs pages are off unless `CRPMS_API_DOCS=1`.

Until 23 September none of this existed: the access library was written and
tested, and nothing called it. `api/test_auth.py` now tests the API itself —
against a server it starts, or with `CRPMS_API_URL` set, against the one that
is running (which is how T-18 uses it).

## One rule for every value

Quality is returned with every value, and a value that is not Good is returned
as `null` with its StatusCode and its reason — never as zero, and never omitted
so that a chart can quietly close the gap. `server_ts` and `transit_ms` are
`null` for a sample no server stamped.
