"""A stand-in Modbus server implementing the bench rig's register map.

WHAT THIS IS FOR, AND WHAT IT IS NOT.

It exists so the BRIDGE can be tested — the decoding, the status-word mapping,
the BadDeviceFailure on a failed sensor — without the ESP32 on the desk. It
implements `firmware/REGISTER_MAP.md`, which is the contract between the
firmware and the bridge.

It is **not** a substitute for the rig, and Stage 10's acceptance test is not
satisfied by running against it. That test is "unplug the temperature probe",
and unplugging a simulated probe proves only that the simulation can be told to
stop. The real test needs the hardware; see
`fat/records/stage-10-hardware-rig.md`.

Nothing downstream of the bridge ever talks to this. The OPC UA simulator is
still a real OPC UA server, as CLAUDE.md requires.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import struct

from sim.bridge import (INVALID_S16, INVALID_U16, IREG_ACS_ZERO_MV, IREG_COUNT,
                        IREG_CURRENT_MA, IREG_SCAN_COUNT, IREG_STATUS,
                        IREG_SUPPLY_MV, IREG_TEMP_AMB_X10, IREG_TEMP_HUB_X10,
                        IREG_VIB_MMS_X100, ST_ACS_OK, ST_ACS_ZEROED,
                        ST_AMBIENT_OK, ST_BUS_OK, ST_HUB_OK, ST_MPU_OK,
                        ST_PROBES_CONFIG, ST_SUPPLY_OK)

log = logging.getLogger("rig_stub")

ALL_OK = (ST_HUB_OK | ST_AMBIENT_OK | ST_MPU_OK | ST_ACS_OK | ST_SUPPLY_OK
          | ST_BUS_OK | ST_ACS_ZEROED | ST_PROBES_CONFIG)

READ_DISCRETE_INPUTS = 2
READ_INPUT_REGISTERS = 4
ILLEGAL_FUNCTION = 1
ILLEGAL_DATA_ADDRESS = 2


class RigStub:
    """Holds the register image the firmware would serve."""

    def __init__(self) -> None:
        self.hub_connected = True
        self.ambient_connected = True
        self.probes_configured = True
        self.running = True
        self.scan = 0
        self.input_registers = [0] * IREG_COUNT
        self.discrete_inputs = [False, False, False]
        self.step()

    def unplug_hub(self) -> None:
        self.hub_connected = False

    def plug_hub(self) -> None:
        self.hub_connected = True

    def step(self) -> None:
        self.scan += 1
        status = ALL_OK
        rng = random.Random(self.scan)

        values = [0] * IREG_COUNT
        # Signed: a fan load reads positive, and a little either side of zero
        # when stopped.
        current = int(460 + rng.gauss(0, 8)) if self.running else int(rng.gauss(0, 6))
        values[IREG_CURRENT_MA] = current & 0xFFFF
        values[IREG_VIB_MMS_X100] = int((240 + rng.gauss(0, 20))
                                        if self.running else 5)
        values[IREG_SUPPLY_MV] = int(12050 + rng.gauss(0, 30))
        values[IREG_ACS_ZERO_MV] = 2503

        if not self.probes_configured:
            status &= ~(ST_PROBES_CONFIG | ST_HUB_OK | ST_AMBIENT_OK)
        if self.hub_connected and self.probes_configured:
            hub = int(round((42.0 + 3 * math.sin(self.scan / 30)
                             + rng.gauss(0, 0.2)) * 10))
        else:
            # Exactly what the firmware does: the sentinel AND the cleared bit.
            hub = INVALID_S16
            status &= ~ST_HUB_OK
        if self.ambient_connected and self.probes_configured:
            ambient = int(round((24.0 + rng.gauss(0, 0.15)) * 10))
        else:
            ambient = INVALID_S16
            status &= ~ST_AMBIENT_OK

        values[IREG_TEMP_HUB_X10] = hub & 0xFFFF
        values[IREG_TEMP_AMB_X10] = ambient & 0xFFFF
        assert INVALID_U16 not in (values[IREG_VIB_MMS_X100],
                                   values[IREG_SUPPLY_MV])
        values[IREG_STATUS] = status
        values[IREG_SCAN_COUNT] = self.scan & 0xFFFF
        self.input_registers = values
        self.discrete_inputs = [self.running, self.running, False]


def _exception(unit: int, function: int, code: int) -> bytes:
    return struct.pack(">BB", function | 0x80, code)


def handle_pdu(rig: RigStub, pdu: bytes) -> bytes:
    """Minimal Modbus TCP server side: input registers and discrete inputs.

    Hand-rolled rather than driven from a library, so the bridge's pymodbus
    CLIENT is exercised against an independent implementation of the wire
    format instead of against the same library's mirror image.
    """
    if len(pdu) < 5:
        return _exception(0, pdu[0] if pdu else 0, ILLEGAL_DATA_ADDRESS)
    function, address, count = struct.unpack(">BHH", pdu[:5])

    if function == READ_INPUT_REGISTERS:
        if count == 0 or address + count > IREG_COUNT:
            return _exception(0, function, ILLEGAL_DATA_ADDRESS)
        payload = b"".join(struct.pack(">H", rig.input_registers[address + i])
                           for i in range(count))
        return struct.pack(">BB", function, len(payload)) + payload

    if function == READ_DISCRETE_INPUTS:
        if count == 0 or address + count > len(rig.discrete_inputs):
            return _exception(0, function, ILLEGAL_DATA_ADDRESS)
        packed = 0
        for i in range(count):
            if rig.discrete_inputs[address + i]:
                packed |= 1 << i
        return struct.pack(">BBB", function, 1, packed)

    return _exception(0, function, ILLEGAL_FUNCTION)


async def _serve_client(rig: RigStub, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            header = await reader.readexactly(7)
            transaction, protocol, length, unit = struct.unpack(">HHHB", header)
            pdu = await reader.readexactly(length - 1)
            response = handle_pdu(rig, pdu)
            writer.write(struct.pack(">HHHB", transaction, 0,
                                     len(response) + 1, unit) + response)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def run(host: str, port: int, interval_s: float = 0.25,
              unplug_after: float | None = None) -> None:
    rig = RigStub()

    async def ticker() -> None:
        elapsed = 0.0
        while True:
            await asyncio.sleep(interval_s)
            elapsed += interval_s
            if unplug_after is not None and elapsed >= unplug_after \
                    and rig.hub_connected:
                log.warning("SIMULATED: hub probe unplugged at t+%.0fs", elapsed)
                rig.unplug_hub()
            rig.step()

    asyncio.create_task(ticker())
    server = await asyncio.start_server(
        lambda r, w: _serve_client(rig, r, w), host, port)
    log.info("rig stand-in on %s:%d (NOT the real rig)", host, port)
    async with server:
        await server.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(prog="firmware.rig_stub")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--unplug-after", type=float, default=None,
                    help="simulate the hub probe being unplugged after N seconds")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    asyncio.run(run(args.host, args.port, unplug_after=args.unplug_after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
