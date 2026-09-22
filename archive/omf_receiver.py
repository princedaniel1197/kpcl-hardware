"""An OMF receiver that writes to TimescaleDB. (Stage 9)

Accepts OMF 1.2 messages so that the collector's only output format can be OMF.
The point is not that this is a PI system — it plainly is not — but that the
collector speaks a real, published, PI-compatible wire format, and pointing it
at an actual PI Web API OMF endpoint is a URL and a token rather than a code
change.

WHAT THIS PRESERVES. A data value's index is its SourceTime, and the receiver
writes it to `sample.source_ts`; ServerTime goes to `server_ts`. Quality is the
numeric StatusCode as sent. A value of null is stored as NULL and never as
zero — the OMF specification would default an omitted numeric property to 0, so
the emitter always sends the property explicitly and the receiver rejects a
message that omits it.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request

from collector.omf import SAMPLE_TYPE_ID, OmfError, validate

log = logging.getLogger("omf.receiver")

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")

INSERT = """
INSERT INTO sample (tag_id, source_ts, server_ts, value, quality)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (tag_id, source_ts) DO NOTHING
"""


class Receiver:
    def __init__(self, dsn: str = DSN) -> None:
        self.dsn = dsn
        self.types: dict[str, dict] = {}
        self.containers: dict[str, str] = {}      # container id -> type id
        self.counts = {"type": 0, "container": 0, "data": 0, "values": 0,
                       "rejected": 0}

    def _tag_id(self, cur, name: str) -> int | None:
        cur.execute("SELECT id FROM tag WHERE name = %s", (name,))
        row = cur.fetchone()
        return row[0] if row else None

    def handle(self, message_type: str, action: str, body: list) -> dict:
        validate(message_type, action, body)
        if message_type == "type":
            for item in body:
                self.types[item["id"]] = item
            self.counts["type"] += len(body)
            return {"types": len(self.types)}

        if message_type == "container":
            for item in body:
                if item.get("typeid") not in self.types:
                    raise OmfError(
                        f"container {item['id']} references type "
                        f"{item.get('typeid')!r}, which has not been created")
                self.containers[item["id"]] = item["typeid"]
            self.counts["container"] += len(body)
            return {"containers": len(self.containers)}

        written = 0
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            for item in body:
                container = item.get("containerid")
                if container is None:
                    continue                      # static/link data: ignored here
                if container not in self.containers:
                    raise OmfError(f"unknown container {container!r}")
                tag_id = self._tag_id(cur, container)
                if tag_id is None:
                    raise OmfError(f"container {container!r} has no matching tag")
                rows = []
                for v in item.get("values", []):
                    rows.append(self._row(tag_id, container, v))
                if rows:
                    cur.executemany(INSERT, rows)
                    written += len(rows)
            conn.commit()
        self.counts["data"] += len(body)
        self.counts["values"] += written
        return {"values": written}

    def _row(self, tag_id: int, container: str, v: dict) -> tuple:
        if "SourceTime" not in v:
            raise OmfError(
                f"{container}: a value with no SourceTime has no measurement "
                "time, and the receipt time must never be substituted for one")
        if "Value" not in v:
            # The specification defaults an omitted numeric property to 0. A
            # Bad sample expressed by omission would therefore be stored as a
            # zero, which is the substitution §318 forbids. Refuse it.
            raise OmfError(
                f"{container}: Value was omitted. OMF would default it to 0; "
                "send an explicit null for a sample that carries no value")
        source_ts = _parse(v["SourceTime"])
        server_ts = _parse(v["ServerTime"]) if v.get("ServerTime") else source_ts
        quality = int(v.get("Quality", 0))
        value = v["Value"]
        if value is not None:
            value = float(value)
        return (tag_id, source_ts, server_ts, value, quality)


def _parse(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise OmfError(f"timestamp {value!r} carries no time zone")
    return parsed.astimezone(dt.timezone.utc)


def build_app(receiver: Receiver | None = None) -> FastAPI:
    receiver = receiver or Receiver()
    app = FastAPI(title="CRPMS OMF receiver",
                  description="Accepts AVEVA OMF 1.2 messages and writes to "
                              "TimescaleDB.",
                  version="1.0")

    @app.post("/omf")
    async def omf(request: Request,
                  messagetype: str = Header(...),
                  action: str = Header("create"),
                  messageformat: str = Header("JSON"),
                  omfversion: str = Header("1.2"),
                  producertoken: str = Header(None)) -> dict:
        if messageformat.upper() != "JSON":
            raise HTTPException(400, "only messageformat JSON is supported")
        if not omfversion.startswith("1."):
            raise HTTPException(400, f"unsupported omfversion {omfversion}")
        body = await request.json()
        try:
            return receiver.handle(messagetype.lower(), action.lower(), body)
        except OmfError as exc:
            receiver.counts["rejected"] += 1
            raise HTTPException(400, str(exc)) from exc

    @app.get("/omf/status")
    async def status() -> dict:
        return {"counts": receiver.counts, "types": sorted(receiver.types),
                "containers": len(receiver.containers)}

    return app


app = build_app()


def main() -> int:
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(prog="archive.omf_receiver")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CRPMS_OMF_PORT", "8082")))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
