"""HTTP control API for the simulator, on a port of its own.

This exists so a fault can be induced during a demonstration without touching
the simulator's logic or restarting anything: force a tag to BadDeviceFailure,
watch it propagate through acquisition, archive and KPI, then restore it.

It forces **quality only**. There is no endpoint that writes a value, and there
must never be one: that would be a control path into the simulated plant, and
this project does not have one anywhere. (§303, §315)
"""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from sim import tags as tagdefs
from sim.quality import FORCEABLE
from sim.server import SimulatorServer


class ForceRequest(BaseModel):
    quality: str = Field(
        description="One of: " + ", ".join(FORCEABLE),
        examples=["BadDeviceFailure"],
    )


def build_app(sim: SimulatorServer) -> FastAPI:
    app = FastAPI(
        title="CRPMS DCS Simulator — control",
        description=(
            "Forces tag quality for demonstration purposes. Quality only: "
            "there is no way to write a value through this API."
        ),
        version="0.1",
    )

    @app.get("/status")
    async def status() -> dict:
        return sim.status()

    @app.get("/scans")
    async def scans(after: int = Query(0, ge=0),
                    up_to: int | None = Query(None, ge=0)) -> dict:
        """The server's own scan timing: work time and lateness against
        schedule, over scans numbered (after, up_to]. Read-only."""
        return sim.scan_summary(after, up_to)

    @app.get("/ledger")
    async def ledger(since: str, until: str, tags: str | None = None) -> dict:
        """What this server published that a subscription must report, per
        tag, between two instants: source timestamps in microseconds since the
        Unix epoch. See sim/ledger.py. Read-only."""
        try:
            lo = dt.datetime.fromisoformat(since)
            hi = dt.datetime.fromisoformat(until)
        except ValueError as exc:
            raise HTTPException(400, f"bad timestamp: {exc}") from exc
        if lo.tzinfo is None or hi.tzinfo is None:
            raise HTTPException(400, "timestamps must carry a time zone")
        names = [t for t in (tags or "").split(",") if t] or None
        oldest = sim.ledger.oldest_us
        return {"since": lo.isoformat(), "until": hi.isoformat(),
                "ledger_oldest_us": oldest,
                "tags": sim.ledger.window(lo, hi, names)}

    @app.get("/tags")
    async def list_tags() -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "unit": t.unit,
                "kind": "digital" if t.is_digital else "analogue",
                "eu_low": t.eu_low,
                "eu_high": t.eu_high,
                "forced_quality": sim.overrides.forced.get(t.name),
                "is_forced": sim.overrides.is_forced(t.name),
            }
            for t in tagdefs.ALL_TAGS
        ]

    @app.get("/quality")
    async def forced() -> dict:
        return {"forced": sim.overrides.forced, "forceable": FORCEABLE}

    @app.post("/quality/{tag}")
    async def force(tag: str, request: ForceRequest) -> dict:
        if tag not in tagdefs.BY_NAME:
            raise HTTPException(404, f"no such tag: {tag}")
        try:
            code = sim.overrides.force(tag, request.quality)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"tag": tag, "quality": request.quality, "status_code": code}

    @app.delete("/quality/{tag}")
    async def restore(tag: str) -> dict:
        if tag not in tagdefs.BY_NAME:
            raise HTTPException(404, f"no such tag: {tag}")
        sim.overrides.clear(tag)
        return {"tag": tag, "quality": "Good", "status_code": 0}

    @app.delete("/quality")
    async def restore_all() -> dict:
        sim.overrides.clear_all()
        return {"forced": {}}

    @app.post("/restart")
    async def restart() -> dict:
        """Run the start-up sequence again from cold. This changes the
        simulated plant's phase, not any tag's value directly."""
        sim.restart()
        return sim.status()

    return app
