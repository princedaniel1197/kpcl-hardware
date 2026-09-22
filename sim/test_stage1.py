"""Stage 1 test.

From the build plan:

    an asyncua client reads U1_MW ten times and prints value, StatusCode name,
    SourceTimestamp and ServerTimestamp. The two timestamps differ on every
    sample. Forcing Bad through the control API changes the StatusCode within
    one scan.

These tests drive a real OPC UA server over a real socket. Nothing is mocked,
because a simulator that is only ever exercised by a test double proves nothing
about the protocol.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest
import uvicorn
from asyncua import Client, ua

from sim import tags as tagdefs
from sim.control import build_app
from sim.server import SimulatorServer

ENDPOINT = "opc.tcp://127.0.0.1:48440/orianode/crpms/test/"
CONTROL_PORT = 48441
SCAN_MS = 100.0


async def control(method: str, path: str, body: dict | None = None):
    """Call the control API over real HTTP.

    Not a test client wrapping the app object: the build plan's test says the
    fault is induced *through the control API*, and an in-process shim would not
    exercise the thing the demonstration actually uses.
    """
    def go():
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{CONTROL_PORT}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None
    return await asyncio.to_thread(go)


@pytest.fixture
async def sim():
    """A running simulator, scanning fast so tests do not crawl."""
    server = SimulatorServer(
        endpoint=ENDPOINT, startup_seconds=8.0, scan_interval_ms=SCAN_MS,
        field_latency_ms=15.0, seed=7,
    )
    await server.init()
    api = uvicorn.Server(uvicorn.Config(
        build_app(server), host="127.0.0.1", port=CONTROL_PORT,
        log_level="error", access_log=False))
    async with server._server:
        server.restart()
        scanner = asyncio.create_task(_scan_forever(server))
        control_task = asyncio.create_task(api.serve())
        await asyncio.sleep(0.5)
        try:
            yield server
        finally:
            api.should_exit = True
            scanner.cancel()
            for task in (scanner, control_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


async def _scan_forever(server: SimulatorServer) -> None:
    while True:
        await server.scan_once()
        await asyncio.sleep(SCAN_MS / 1000.0)


async def _node(client: Client, name: str):
    idx = await client.get_namespace_index("urn:orianode:crpms:sim")
    return await client.nodes.root.get_child(
        ["0:Objects", f"{idx}:Unit1", f"{idx}:{name}"]
    )


async def test_ten_reads_of_u1_mw_have_distinct_timestamps(sim, capsys):
    """The stage test, exactly as the build plan words it."""
    async with Client(url=ENDPOINT) as client:
        node = await _node(client, "U1_MW")

        # Sample while the unit is actually loading. The build plan's rule is
        # that the two timestamps differ *on a changing tag*; reading a tag
        # pinned at zero would not test what it claims to.
        deadline = asyncio.get_running_loop().time() + 12.0
        while await node.read_value() <= 0.0:
            assert asyncio.get_running_loop().time() < deadline, (
                "unit never took load")
            await asyncio.sleep(0.1)

        samples = []
        for _ in range(10):
            dv = await node.read_data_value()
            samples.append(dv)
            await asyncio.sleep(SCAN_MS / 1000.0 * 1.5)

    print(f"\n{'value':>10}  {'StatusCode':<12} {'SourceTimestamp':<30} "
          f"{'ServerTimestamp':<30} {'delta_ms':>9}")
    for dv in samples:
        delta = (dv.ServerTimestamp - dv.SourceTimestamp).total_seconds() * 1000
        print(f"{dv.Value.Value:>10.3f}  {dv.StatusCode.name:<12} "
              f"{dv.SourceTimestamp!s:<30} {dv.ServerTimestamp!s:<30} {delta:>9.2f}")

    assert len(samples) == 10
    # The tag really was changing across the ten reads.
    values = [dv.Value.Value for dv in samples]
    assert len(set(values)) > 1, "U1_MW did not change; this proves nothing"
    for dv in samples:
        assert dv.SourceTimestamp is not None
        assert dv.ServerTimestamp is not None
        # The whole point of §335.
        assert dv.SourceTimestamp != dv.ServerTimestamp
        # Measurement precedes publication; the reverse would mean the source
        # timestamp had been invented at write time.
        assert dv.ServerTimestamp > dv.SourceTimestamp


async def test_source_timestamp_is_not_receipt_time(sim):
    """A client reading the same unchanged value twice must see the same
    SourceTimestamp. If it moved, it would be receipt time wearing a
    measurement time's name."""
    async with Client(url=ENDPOINT) as client:
        node = await _node(client, "U1_MW")
        first = await node.read_data_value()
        await asyncio.sleep(0.25)
        second = await node.read_data_value()
        third = await node.read_data_value()

    assert second.SourceTimestamp == third.SourceTimestamp
    assert second.ServerTimestamp == third.ServerTimestamp
    assert first.SourceTimestamp <= second.SourceTimestamp


async def test_forcing_bad_takes_effect_within_one_scan(sim):
    """Forcing Bad through the control API changes the StatusCode within one
    scan, and the tag recovers when restored."""
    async with Client(url=ENDPOINT) as client:
        node = await _node(client, "U1_COAL_FLOW")
        before = await node.read_data_value()
        assert before.StatusCode.name == "Good"

        status, payload = await control(
            "POST", "/quality/U1_COAL_FLOW", {"quality": "BadDeviceFailure"})
        assert status == 200
        assert payload["status_code"] == int(ua.StatusCodes.BadDeviceFailure)

        await asyncio.sleep(SCAN_MS / 1000.0 * 2)
        during = await node.read_data_value(raise_on_bad_status=False)
        assert during.StatusCode.name == "BadDeviceFailure"
        assert during.StatusCode.is_bad()
        # Quality went Bad; the source timestamp is still a real one.
        assert during.SourceTimestamp != during.ServerTimestamp

        await control("DELETE", "/quality/U1_COAL_FLOW")
        await asyncio.sleep(SCAN_MS / 1000.0 * 2)
        after = await node.read_data_value()
        assert after.StatusCode.name == "Good"


async def test_forcing_uncertain(sim):
    await control("POST", "/quality/U1_BEARING_VIB",
                  {"quality": "UncertainSensorNotAccurate"})
    async with Client(url=ENDPOINT) as client:
        node = await _node(client, "U1_BEARING_VIB")
        await asyncio.sleep(SCAN_MS / 1000.0 * 2)
        dv = await node.read_data_value(raise_on_bad_status=False)
        assert dv.StatusCode.name == "UncertainSensorNotAccurate"
        # Uncertain is its own thing: neither Good nor Bad.
        assert not dv.StatusCode.is_bad()
        assert dv.StatusCode.value != 0


async def test_forcing_one_tag_does_not_affect_others(sim):
    await control("POST", "/quality/U1_MS_TEMP", {"quality": "BadDeviceFailure"})
    await asyncio.sleep(SCAN_MS / 1000.0 * 2)
    async with Client(url=ENDPOINT) as client:
        bad = await (await _node(client, "U1_MS_TEMP")).read_data_value(
            raise_on_bad_status=False)
        other = await (await _node(client, "U1_MS_PRESS")).read_data_value()
    assert bad.StatusCode.is_bad()
    assert other.StatusCode.name == "Good"


async def test_every_tag_exists_with_units_and_eurange(sim):
    """Engineering units and an EURange on every analogue (§436). A tag without
    a range cannot be range-checked, which Stage 6 depends on."""
    async with Client(url=ENDPOINT) as client:
        idx = await client.get_namespace_index("urn:orianode:crpms:sim")
        for spec in tagdefs.ALL_TAGS:
            node = await _node(client, spec.name)
            assert await node.read_value() is not None
            if spec.is_digital:
                continue
            eu_range = await node.get_child([f"{idx}:EURange"])
            value = await eu_range.read_value()
            assert value.Low == spec.eu_low
            assert value.High == spec.eu_high
            units = await node.get_child([f"{idx}:EngineeringUnits"])
            assert (await units.read_value()).DisplayName.Text == spec.unit


async def test_digitals_are_written_only_on_change(sim):
    """Digitals change state; they are not sampled analogues (§440). Over many
    scans, a digital that has not transitioned keeps the SourceTimestamp of the
    transition that set it."""
    async with Client(url=ENDPOINT) as client:
        node = await _node(client, "U1_BOILER_LIGHTUP")
        first = await node.read_data_value()
        scans_before = sim._scan_count
        await asyncio.sleep(SCAN_MS / 1000.0 * 4)
        second = await node.read_data_value()

    assert sim._scan_count > scans_before   # scans did happen
    if first.Value.Value == second.Value.Value:
        assert first.SourceTimestamp == second.SourceTimestamp, (
            "a digital that did not change was rewritten anyway"
        )


async def test_analogues_are_written_every_scan(sim):
    """Analogues are sampled, so their SourceTimestamp advances."""
    async with Client(url=ENDPOINT) as client:
        node = await _node(client, "U1_DRUM_PRESS")
        first = await node.read_data_value()
        await asyncio.sleep(SCAN_MS / 1000.0 * 3)
        second = await node.read_data_value()
    assert second.SourceTimestamp > first.SourceTimestamp


async def test_tags_are_not_writable_by_clients(sim):
    """The simulated plant has no control path. A client may read; it may not
    write. (§303, §315)"""
    async with Client(url=ENDPOINT) as client:
        node = await _node(client, "U1_MW")
        with pytest.raises(ua.UaStatusCodeError):
            await node.write_value(ua.Variant(123.0, ua.VariantType.Double))


async def test_control_api_offers_no_way_to_set_a_value(sim):
    """Inspection of the control surface: quality only, by construction."""
    app = build_app(sim)
    paths = {route.path for route in app.routes}
    assert paths >= {"/status", "/tags", "/quality", "/quality/{tag}", "/restart"}
    for path in paths:
        assert "value" not in path.lower()


async def test_unknown_tag_and_unknown_quality_are_refused(sim):
    status, _ = await control("POST", "/quality/NO_SUCH_TAG",
                              {"quality": "BadDeviceFailure"})
    assert status == 404
    status, _ = await control("POST", "/quality/U1_MW", {"quality": "SlightlyOff"})
    assert status == 400


async def test_startup_reaches_full_load_and_phases_are_ordered(sim):
    """The unit actually starts: breaker closes, load arrives, speed is rated."""
    sim.restart()
    seen: list[str] = []
    deadline = asyncio.get_running_loop().time() + 14.0
    while asyncio.get_running_loop().time() < deadline:
        phase = sim.plant.phase_at(sim.elapsed_s)[0].value
        if not seen or seen[-1] != phase:
            seen.append(phase)
        if phase == "STEADY":
            break
        await asyncio.sleep(0.1)

    from sim.plant import PHASE_ORDER
    full = [p.value for p in PHASE_ORDER]
    # Contiguous and in order, through to STEADY: no phase skipped, none
    # revisited.
    assert seen == full[full.index(seen[0]):]
    assert seen[-1] == "STEADY"
    assert "OFFLINE" in seen and "SYNCHRONISATION" in seen

    async with Client(url=ENDPOINT) as client:
        mw = await (await _node(client, "U1_MW")).read_value()
        speed = await (await _node(client, "U1_TURB_SPEED")).read_value()
        breaker = await (await _node(client, "U1_BREAKER_CLOSED")).read_value()
    assert breaker is True
    assert 195 <= mw <= 225
    assert 2950 <= speed <= 3050


async def test_reading_a_bad_value_requires_raise_on_bad_status_false(sim):
    """asyncua's client raises UaStatusCodeError when it reads a non-Good
    status. That is the default, and it fires on exactly the values this
    project exists to preserve. Recorded here so Stage 3's collector is written
    knowing it."""
    await control("POST", "/quality/U1_AUX_POWER", {"quality": "BadDeviceFailure"})
    await asyncio.sleep(SCAN_MS / 1000.0 * 2)
    async with Client(url=ENDPOINT) as client:
        node = await _node(client, "U1_AUX_POWER")
        with pytest.raises(ua.UaStatusCodeError):
            await node.read_data_value()
        dv = await node.read_data_value(raise_on_bad_status=False)
        assert dv.StatusCode.name == "BadDeviceFailure"
        assert dv.SourceTimestamp is not None
        assert dv.SourceTimestamp != dv.ServerTimestamp
