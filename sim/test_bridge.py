"""Bridge tests (Stage 10).

These test the BRIDGE — the decoding and the status-word mapping. They do not
test the rig, and passing them is not Stage 10's acceptance criterion. That
criterion is "unplug the temperature probe", and unplugging a simulated probe
proves only that the simulation can be told to stop.
"""

from __future__ import annotations

import struct

import pytest
from asyncua import ua

from firmware.rig_stub import ALL_OK, RigStub, handle_pdu
from sim import bridge

GOOD = 0
BAD_DEVICE = int(ua.StatusCodes.BadDeviceFailure)
BAD_CONFIG = int(ua.StatusCodes.BadConfigurationError)
BAD_RANGE = int(ua.StatusCodes.BadOutOfRange)


def registers(hub=425, ambient=240, status=ALL_OK, current=460, vib=234,
              supply=12050) -> list[int]:
    words = [0] * bridge.IREG_COUNT
    words[bridge.IREG_CURRENT_MA] = current & 0xFFFF
    words[bridge.IREG_VIB_MMS_X100] = vib
    words[bridge.IREG_TEMP_HUB_X10] = hub & 0xFFFF
    words[bridge.IREG_TEMP_AMB_X10] = ambient & 0xFFFF
    words[bridge.IREG_SUPPLY_MV] = supply
    words[bridge.IREG_STATUS] = status
    return words


def point(tag: str) -> bridge.Point:
    return next(p for p in bridge.POINTS if p.tag == tag)


# --- scaling -----------------------------------------------------------------

def test_registers_scale_to_engineering_units():
    words = registers()
    assert bridge.decode(point("RIG_CURRENT"), words, ALL_OK)[0] == pytest.approx(0.460)
    assert bridge.decode(point("RIG_VIBRATION"), words, ALL_OK)[0] == pytest.approx(2.34)
    assert bridge.decode(point("RIG_HUB_TEMP"), words, ALL_OK)[0] == pytest.approx(42.5)
    assert bridge.decode(point("RIG_SUPPLY_V"), words, ALL_OK)[0] == pytest.approx(12.05)


def test_temperature_registers_are_signed():
    """A probe below 0 degC is a real reading; an unsigned register would wrap
    it to something enormous."""
    words = registers(hub=-55)          # -5.5 degC
    value, quality, _ = bridge.decode(point("RIG_HUB_TEMP"), words, ALL_OK)
    assert value == pytest.approx(-5.5)
    assert quality == GOOD


def test_current_is_signed_so_a_wrong_zero_shows():
    """Register map v2. A fan load cannot run backwards; a negative current is
    a wrong zero, and an unsigned register (or an RMS) would have hidden it."""
    words = registers(current=-37)
    value, quality, _ = bridge.decode(point("RIG_CURRENT"), words, ALL_OK)
    assert value == pytest.approx(-0.037) and quality == GOOD


# --- the failure mapping, which is the point ---------------------------------

@pytest.mark.parametrize("tag, sentinel", [
    ("RIG_CURRENT", bridge.INVALID_S16), ("RIG_VIBRATION", bridge.INVALID_U16),
    ("RIG_HUB_TEMP", bridge.INVALID_S16), ("RIG_SUPPLY_V", bridge.INVALID_U16)])
def test_every_channel_has_a_sentinel_the_bridge_refuses(tag, sentinel):
    """Register map v2: zero is what a stopped fan reads, so no channel reports
    a failure as zero any more."""
    p = point(tag)
    words = registers()
    words[p.register] = sentinel & 0xFFFF
    value, quality, reason = bridge.decode(p, words, ALL_OK)
    assert value is None and quality == BAD_DEVICE and "disagree" in reason


def test_unconfigured_probe_addresses_say_so():
    """Without configured ROM addresses the firmware cannot know which probe is
    the hub, and says so; the bridge maps that to BadConfigurationError, not to
    a sensor failure that sends someone to check the wiring."""
    status = ALL_OK & ~(bridge.ST_PROBES_CONFIG | bridge.ST_HUB_OK
                        | bridge.ST_AMBIENT_OK)
    words = registers(hub=bridge.INVALID_S16, ambient=bridge.INVALID_S16,
                      status=status)
    for tag in ("RIG_HUB_TEMP", "RIG_AMBIENT_TEMP"):
        value, quality, reason = bridge.decode(point(tag), words, status)
        assert value is None and quality == BAD_CONFIG and "ROM" in reason
    assert bridge.decode(point("RIG_CURRENT"), words, status)[1] == GOOD


def test_a_supply_beyond_the_adc_is_out_of_range_not_a_device_failure():
    status = ALL_OK & ~bridge.ST_SUPPLY_OK
    words = registers(supply=bridge.INVALID_U16, status=status)
    value, quality, _ = bridge.decode(point("RIG_SUPPLY_V"), words, status)
    assert value is None and quality == BAD_RANGE


def test_a_low_supply_is_still_a_measurement():
    """9.8 V during a brown-out is the reading worth having. The firmware no
    longer clears the bit for it; judging it is the quality engine's job."""
    words = registers(supply=9800)
    value, quality, _ = bridge.decode(point("RIG_SUPPLY_V"), words, ALL_OK)
    assert value == pytest.approx(9.8) and quality == GOOD


def test_a_clear_status_bit_becomes_bad_device_failure():
    words = registers(hub=bridge.TEMP_INVALID)
    value, quality, reason = bridge.decode(
        point("RIG_HUB_TEMP"), words, ALL_OK & ~bridge.ST_HUB_OK)
    assert value is None
    assert quality == BAD_DEVICE
    assert "status bit" in reason


def test_a_failed_sensor_does_not_affect_the_others():
    """Per-point quality, not all-or-nothing. This is why the firmware uses a
    status word rather than a Modbus exception: an exception would deny the
    master every register in the request, including the ones still good."""
    status = ALL_OK & ~bridge.ST_HUB_OK
    words = registers(hub=bridge.TEMP_INVALID, status=status)
    assert bridge.decode(point("RIG_HUB_TEMP"), words, status)[1] == BAD_DEVICE
    for tag in ("RIG_CURRENT", "RIG_VIBRATION", "RIG_AMBIENT_TEMP",
                "RIG_SUPPLY_V"):
        assert bridge.decode(point(tag), words, status)[1] == GOOD


def test_a_failed_sensor_publishes_no_value_at_all():
    """Not zero, not the last one."""
    words = registers(hub=bridge.TEMP_INVALID)
    value, _, _ = bridge.decode(point("RIG_HUB_TEMP"), words,
                                ALL_OK & ~bridge.ST_HUB_OK)
    assert value is None


def test_the_sentinel_is_caught_even_if_the_status_bit_lies():
    """Belt and braces. If the firmware and the bridge disagree, say so rather
    than silently trusting one of them."""
    words = registers(hub=bridge.TEMP_INVALID)
    value, quality, reason = bridge.decode(point("RIG_HUB_TEMP"), words, ALL_OK)
    assert value is None
    assert quality == BAD_DEVICE
    assert "disagree" in reason


def test_the_ds18b20_power_on_value_would_be_plausible_and_is_not_trusted():
    """85.0 degC is the DS18B20's power-on-reset reading and an entirely
    credible motor hub temperature. Recognising it is the FIRMWARE's job -- it
    is the only side that knows the probe was read -- and it clears the bit.
    This asserts the bridge then refuses the register. (With the bit set the
    bridge cannot tell a reset from a reading; that is why the firmware must.)"""
    words = registers(hub=850)                       # 85.0 degC
    _, quality, _ = bridge.decode(point("RIG_HUB_TEMP"), words,
                                  ALL_OK & ~bridge.ST_HUB_OK)
    assert quality == BAD_DEVICE


# --- the stand-in implements the documented contract -------------------------

def test_the_stand_in_serves_input_registers():
    rig = RigStub()
    pdu = struct.pack(">BHH", 4, 0, bridge.IREG_COUNT)
    response = handle_pdu(rig, pdu)
    assert response[0] == 4
    assert response[1] == bridge.IREG_COUNT * 2


def test_the_stand_in_rejects_an_out_of_range_read():
    rig = RigStub()
    response = handle_pdu(rig, struct.pack(">BHH", 4, 0, 99))
    assert response[0] == 4 | 0x80           # exception
    assert response[1] == 2                  # ILLEGAL_DATA_ADDRESS


def test_the_stand_in_rejects_an_unsupported_function():
    rig = RigStub()
    response = handle_pdu(rig, struct.pack(">BHH", 3, 0, 1))   # holding regs
    assert response[0] == 3 | 0x80
    assert response[1] == 1                  # ILLEGAL_FUNCTION


def test_unplugging_clears_the_bit_and_writes_the_sentinel():
    """The two halves of the firmware's behaviour, together."""
    rig = RigStub()
    rig.unplug_hub()
    rig.step()
    assert not (rig.input_registers[bridge.IREG_STATUS] & bridge.ST_HUB_OK)
    raw = rig.input_registers[bridge.IREG_TEMP_HUB_X10]
    assert bridge._signed(raw) == bridge.TEMP_INVALID


def test_plugging_back_in_restores_it():
    rig = RigStub()
    rig.unplug_hub(); rig.step()
    rig.plug_hub(); rig.step()
    assert rig.input_registers[bridge.IREG_STATUS] & bridge.ST_HUB_OK


# --- the acquisition path stays read-only ------------------------------------

def test_the_bridge_is_not_in_the_collector_package():
    """The bridge writes to the OPC UA address space, so it lives in sim/. In
    collector/ it would put a write capability inside the package required to
    have none (§303, §315)."""
    from pathlib import Path
    assert (Path(bridge.__file__).parent.name) == "sim"


def test_no_modbus_write_is_issued_by_the_bridge():
    """The rig has coils, and nothing in the monitoring path ever operates
    them."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(bridge.__file__).read_text())
    calls = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    for forbidden in ("write_coil", "write_coils", "write_register",
                      "write_registers"):
        assert forbidden not in calls


# --- end to end: stand-in -> pymodbus -> bridge -> OPC UA ---------------------

async def test_a_failed_probe_reaches_opc_ua_as_bad_with_no_value():
    """The whole path the bench rig will use, against the stand-in: the rig's
    register image over real Modbus TCP, decoded by the bridge, published into
    a real OPC UA server, read back by a real client."""
    import asyncio
    import socket
    from asyncua import Client, Server

    from firmware import rig_stub

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        modbus_port = probe.getsockname()[1]
    rig = RigStub()
    modbus = await asyncio.start_server(
        lambda r, w: rig_stub._serve_client(rig, r, w), "127.0.0.1", modbus_port)

    server = Server()
    await server.init()
    endpoint = "opc.tcp://127.0.0.1:48460/bridge-test/"
    server.set_endpoint(endpoint)
    idx = await server.register_namespace("urn:orianode:crpms:sim")
    b = bridge.ModbusBridge(server, idx, "127.0.0.1", modbus_port)
    await b.build_address_space()
    async with server, modbus:
        client = Client(endpoint)
        await client.connect()
        try:
            async def read(tag):
                node = await client.nodes.objects.get_child(
                    [f"{idx}:BenchRig", f"{idx}:{tag}"])
                return await node.read_data_value(raise_on_bad_status=False)

            assert await b.poll_once()
            good = await read("RIG_HUB_TEMP")
            assert good.StatusCode.value == GOOD
            assert good.Value.Value == pytest.approx(
                bridge._signed(rig.input_registers[bridge.IREG_TEMP_HUB_X10]) / 10)

            rig.unplug_hub()
            rig.step()
            assert await b.poll_once()
            bad = await read("RIG_HUB_TEMP")
            assert bad.StatusCode.value == BAD_DEVICE
            assert bad.Value.Value is None
            # And only the hub.
            assert (await read("RIG_AMBIENT_TEMP")).StatusCode.value == GOOD
            assert (await read("RIG_CURRENT")).StatusCode.value == GOOD
        finally:
            b.close()
            await client.disconnect()


def test_the_bridge_takes_tcp_or_rtu_but_not_both():
    with pytest.raises(ValueError):
        bridge.ModbusBridge(None, 2, host="10.0.0.9", serial_port="/dev/ttyUSB0")
    with pytest.raises(ValueError):
        bridge.ModbusBridge(None, 2)
    rtu = bridge.ModbusBridge(None, 2, serial_port="/dev/ttyUSB0", baudrate=19200)
    assert "RTU" in rtu.where and "19200" in rtu.where
