"""OPC UA server standing in for a DCS.

Everything downstream in this project talks to this server over real OPC UA.
It is never replaced by mock data or a test fixture, because a stack that has
only ever been fed convenient data has not been demonstrated at all.

Three decisions are made here and are not negotiable.

SourceTimestamp is the instant the simulator computed the value. ServerTimestamp
is the instant it went into the address space. They are different quantities and
on a changing tag they are never equal. Measured against asyncua 2.0.1: a
SourceTimestamp written on a DataValue is preserved exactly, and the server
stamps ServerTimestamp itself at write time — a sentinel written into that field
is discarded. So we set SourceTimestamp and let the server own ServerTimestamp,
which is both correct OPC UA semantics and more truthful than fabricating one.
(§335)

Quality is a real StatusCode carried on the DataValue, not a boolean or a
string. (§318)

Digitals change state; they are not sampled analogues. A digital is written when
it changes and at no other time, so a subscriber sees transitions rather than a
stream of identical values. (§440)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time

from asyncua import Server, ua

from sim import tags as tagdefs
from sim.plant import Phase, Plant
from sim.quality import QualityOverrides

log = logging.getLogger("sim")

DEFAULT_ENDPOINT = "opc.tcp://0.0.0.0:4840/orianode/crpms/"
NAMESPACE_URI = "urn:orianode:crpms:sim"
SERVER_NAME = "Orianode CRPMS DCS Simulator"


class SimulatorServer:
    """A 210 MW unit, exposed over OPC UA and driven through a cold start-up."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        startup_seconds: float = 90.0,
        scan_interval_ms: float = 500.0,
        field_latency_ms: float = 40.0,
        seed: int | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.scan_interval_s = scan_interval_ms / 1000.0

        # Models the transit between a field device producing a value and the
        # DCS server publishing it. Real plants have it; it is why measurement
        # time and publication time are separate quantities in the first place.
        # Setting this to zero does not make the two timestamps equal, it just
        # makes the gap uninteresting.
        self.field_latency_s = field_latency_ms / 1000.0

        self.plant = Plant(startup_seconds=startup_seconds, seed=seed)
        self.overrides = QualityOverrides()

        self._server = Server()
        self._rng = random.Random(seed)
        self._nodes: dict[str, object] = {}
        self._last_digital: dict[str, bool | None] = {
            t.name: None for t in tagdefs.DIGITALS
        }
        self._started_monotonic: float | None = None
        self._scan_count = 0
        self._digital_writes = 0

    # -- lifecycle ---------------------------------------------------------

    async def init(self) -> None:
        await self._server.init()
        self._server.set_endpoint(self.endpoint)
        self._server.set_server_name(SERVER_NAME)
        idx = await self._server.register_namespace(NAMESPACE_URI)
        self.namespace_index = idx

        unit = await self._server.nodes.objects.add_object(idx, "Unit1")
        self._unit_node = unit

        for spec in tagdefs.ALL_TAGS:
            initial = False if spec.is_digital else 0.0
            node = await unit.add_variable(idx, spec.name, initial, spec.variant_type)
            await node.set_writable(False)   # nothing outside may write a value
            self._nodes[spec.name] = node
            await self._add_metadata(node, spec, idx)

        log.info("address space built: %d tags under Unit1", len(self._nodes))

    async def _add_metadata(self, node, spec: tagdefs.TagSpec, idx: int) -> None:
        """Engineering units and EURange on every tag (§436).

        EURange is the instrument span. Stage 6 range checks are checked against
        it, so a tag without one cannot be range checked at all.
        """
        await node.add_property(idx, "Description", spec.description)
        if spec.is_digital:
            return
        await node.add_property(
            idx, "EngineeringUnits",
            ua.EUInformation(
                NamespaceUri=NAMESPACE_URI,
                UnitId=0,
                DisplayName=ua.LocalizedText(spec.unit),
                Description=ua.LocalizedText(spec.description),
            ),
        )
        await node.add_property(
            idx, "EURange",
            ua.Range(Low=spec.eu_low, High=spec.eu_high),
        )

    # -- the scan ----------------------------------------------------------

    @property
    def elapsed_s(self) -> float:
        if self._started_monotonic is None:
            return 0.0
        return time.monotonic() - self._started_monotonic

    def restart(self) -> None:
        """Begin the start-up sequence again from cold."""
        self._started_monotonic = time.monotonic()
        self._last_digital = {t.name: None for t in tagdefs.DIGITALS}
        log.info("start-up sequence restarted")

    async def scan_once(self) -> None:
        """One scan: compute, wait out the field latency, publish."""
        state = self.plant.state_at(self.elapsed_s)

        # The instant the simulator computed these values. Everything in one
        # scan was computed together, so they share it. This is never replaced
        # by the time the value was written or received, anywhere, ever.
        source_ts = dt.datetime.now(dt.timezone.utc)

        # Field-to-server transit, with a little jitter. The write that follows
        # is what the server stamps as ServerTimestamp.
        if self.field_latency_s > 0:
            await asyncio.sleep(
                max(0.0, self._rng.gauss(self.field_latency_s,
                                         self.field_latency_s * 0.15))
            )

        for name, value in state.analogues.items():
            await self._write(name, value, source_ts, ua.VariantType.Double)

        # Digitals are written only when they change (§440).
        for name, value in state.digitals.items():
            if self._last_digital[name] != value:
                await self._write(name, value, source_ts, ua.VariantType.Boolean)
                self._last_digital[name] = value
                self._digital_writes += 1
                log.info("digital %s -> %s (%s)", name, value, state.phase.value)

        self._scan_count += 1

    async def _write(self, name, value, source_ts, variant_type) -> None:
        """Write one tag as a DataValue carrying its own quality and source
        time. ServerTimestamp is deliberately not set: the server owns it and
        overwrites anything we put there."""
        node = self._nodes[name]
        status = self.overrides.status_for(name)
        await node.write_value(
            ua.DataValue(
                Value=ua.Variant(value, variant_type),
                StatusCode=ua.StatusCode(status),
                SourceTimestamp=source_ts,
            )
        )

    async def run(self) -> None:
        """Serve, and scan on a schedule that does not drift."""
        async with self._server:
            self.restart()
            log.info("serving on %s", self.endpoint)
            origin = time.monotonic()
            tick = 0
            last_phase: Phase | None = None
            while True:
                due = origin + tick * self.scan_interval_s
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.scan_once()

                phase = self.plant.phase_at(self.elapsed_s)[0]
                if phase is not last_phase:
                    log.info("phase: %s (t=%.1fs)", phase.value, self.elapsed_s)
                    last_phase = phase
                tick += 1

    # -- introspection for the control API ---------------------------------

    def status(self) -> dict:
        phase, fraction = self.plant.phase_at(self.elapsed_s)
        return {
            "endpoint": self.endpoint,
            "elapsed_s": round(self.elapsed_s, 2),
            "startup_seconds": self.plant.startup_seconds,
            "startup_complete_s": self.plant.startup_complete_s,
            "phase": phase.value,
            "phase_fraction": round(fraction, 4),
            "scan_interval_ms": self.scan_interval_s * 1000,
            "field_latency_ms": self.field_latency_s * 1000,
            "scans": self._scan_count,
            "digital_writes": self._digital_writes,
            "forced": self.overrides.forced,
        }
