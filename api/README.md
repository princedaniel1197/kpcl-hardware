# api — FastAPI and WebSocket (Stage 12)

REST endpoints for tags, trends, asset hierarchy, KPI values, event frames, tag
health and replay, plus a WebSocket relaying the collector's event stream to the
browser. FastAPI is the only web framework in this project.

```bash
make api       # uvicorn on 127.0.0.1:8000
```

## No access control

There is no sign-in. Token access and role-based access (§509) — a bearer token
on every route, a permission table, a station scope — were removed from the API
and the UI by decision on 24 Sep 2026, with `api/auth.py` and `api/test_auth.py`
and FAT test T-18. Anyone who can reach the API reads everything it serves. It
listens on 127.0.0.1 by default; do not expose it beyond the station laptop
without putting access control back in front of it. The access library
(`ops/access.py`, the `principal` table) is still there, used by nothing in the
API; the history of how it was enforced is in `fat/records/`.

CORS allows only the UI's origins (`CRPMS_UI_ORIGINS`) and GET. The generated
schema and docs pages are off unless `CRPMS_API_DOCS=1`.

## One rule for every value

Quality is returned with every value, and a value that is not Good is returned
as `null` with its StatusCode and its reason — never as zero, and never omitted
so that a chart can quietly close the gap. `server_ts` and `transit_ms` are
`null` for a sample no server stamped.
