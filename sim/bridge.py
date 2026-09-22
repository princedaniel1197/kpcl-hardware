"""Modbus-to-OPC UA bridge for the bench rig. (Stage 10)

Reads the ESP32 over Modbus TCP with pymodbus and republishes into the Stage 1
OPC UA server's address space as a second unit, so that **the collector requires
no change**: it already subscribes to whatever tags the tag table lists, and the
rig's tags were created from a template by Stage 5.

THE MAPPING THAT MATTERS. The firmware's status word says, per sensor, whether
this scan's reading can be trusted. A clear bit becomes `BadDeviceFailure` on
that tag and nothing else — the value is not published, the previous value is
not republished, and no zero is invented. That is §318 crossing a fieldbus
boundary, which is the one place it is easiest to lose.

The bridge lives in sim/ rather than collector/ because it is a source of data,
not an acquirer of it. Putting it in collector/ would also put a Modbus write
capability inside the package that is required to have no write path at all.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass

from asyncua import ua

log = logging.getLogger("sim.bridge")

# Register map: firmware/REGISTER_MAP.md is the contract.
IREG_CURRENT_MA = 0
IREG_VIB_MMS_X100 = 1
IREG_TEMP_HUB_X10 = 2
IREG_TEMP_AMB_X10 = 3
IREG_SUPPLY_MV = 4
IREG_STATUS = 5
IREG_SCAN_COUNT = 6
IREG_COUNT = 7

ST_HUB_OK = 1 << 0
ST_AMBIENT_OK = 1 << 1
ST_MPU_OK = 1 << 2
ST_ACS_OK = 1 << 3
ST_SUPPLY_OK = 1 << 4
ST_BUS_OK = 1 << 5

TEMP_INVALID = -32768

GOOD = int(ua.StatusCodes.Good)
BAD_DEVICE_FAILURE = int(ua.StatusCodes.BadDeviceFailure)
BAD_NO_COMMUNICATION = int(ua.StatusCodes.BadNoCommunication)


@dataclass(frozen=True)
class Point:
    """One published quantity: where it comes from, what it means, how to know
    whether to believe it."""
    tag: str              # OPC UA node name, e.g. RIG_HUB_TEMP
    register: int
    scale: float
    unit: str
    description: str
    status_bit: int
    signed: bool = False
    eu_low: float = 0.0
    eu_high: float = 100.0


POINTS: tuple[Point, ...] = (
    Point("RIG_CURRENT", IREG_CURRENT_MA, 0.001, "A",
          "Bench rig total load current (ACS712 5 A)", ST_ACS_OK,
          eu_low=0.0, eu_high=5.0),
    Point("RIG_VIBRATION", IREG_VIB_MMS_X100, 0.01, "mm/s",
          "Bench rig fan vibration (MPU-6050, approximate velocity)",
          ST_MPU_OK, eu_low=0.0, eu_high=50.0),
    Point("RIG_HUB_TEMP", IREG_TEMP_HUB_X10, 0.1, "degC",
          "Fan motor hub temperature (DS18B20)", ST_HUB_OK, signed=True,
          eu_low=-55.0, eu_high=125.0),
    Point("RIG_AMBIENT_TEMP", IREG_TEMP_AMB_X10, 0.1, "degC",
          "Ambient temperature (DS18B20)", ST_AMBIENT_OK, signed=True,
          eu_low=-55.0, eu_high=125.0),
    Point("RIG_SUPPLY_V", IREG_SUPPLY_MV, 0.001, "V",
          "Bench rig supply voltage", ST_SUPPLY_OK, eu_low=0.0, eu_high=15.0),
)

DIGITALS = (("RIG_RUNNING", 0, "Rig run/stop state"),
            ("RIG_RELAY_1", 1, "Relay group 1 energised"),
            ("RIG_RELAY_2", 2, "Relay group 2 energised"))


def _signed(word: int) -> int:
    return word - 0x10000 if word >= 0x8000 else word


def decode(point: Point, registers: list[int], status: int
           ) -> tuple[float | None, int, str | None]:
    """Turn one raw register into a value and a StatusCode.

    Returns (value, quality, reason). A value is returned only when the
    firmware says the sensor was read successfully this scan. Otherwise the
    value is None — not the last one, not zero.
    """
    if not (status & point.status_bit):
        return None, BAD_DEVICE_FAILURE, (
            f"firmware status bit for {point.tag} is clear: the sensor was not "
            f"read successfully this scan")
    raw = registers[point.register]
    if point.signed:
        raw = _signed(raw)
        if raw == TEMP_INVALID:
            # Belt and braces: the status bit should already have caught this.
            # If it did not, the firmware and the bridge disagree, which is
            # worth saying rather than silently trusting one of them.
            return None, BAD_DEVICE_FAILURE, (
                f"{point.tag} holds the invalid sentinel although its status "
                f"bit is set — firmware and bridge disagree")
    return raw * point.scale, GOOD, None


class ModbusBridge:
    """Polls the rig and publishes into an existing OPC UA server."""

    def __init__(self, server, namespace_index: int, host: str,
                 port: int = 502, unit_id: int = 1,
                 poll_interval_s: float = 1.0) -> None:
        self.server = server
        self.idx = namespace_index
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.poll_interval_s = poll_interval_s
        self._nodes: dict[str, object] = {}
        self._client = None
        self.polls = 0
        self.failures = 0

    async def build_address_space(self) -> None:
        """Create the rig's nodes under their own object.

        The rig is a bench rig and its tags say so: amperes, millimetres per
        second, degrees Celsius. Nothing is scaled or renamed to resemble a
        210 MW unit.
        """
        objects = self.server.nodes.objects
        rig = await objects.add_object(self.idx, "BenchRig")
        for point in POINTS:
            node = await rig.add_variable(self.idx, point.tag, 0.0,
                                          ua.VariantType.Double)
            await node.set_writable(False)
            await node.add_property(self.idx, "Description", point.description)
            await node.add_property(
                self.idx, "EngineeringUnits",
                ua.EUInformation(NamespaceUri="urn:orianode:crpms:sim", UnitId=0,
                                 DisplayName=ua.LocalizedText(point.unit),
                                 Description=ua.LocalizedText(point.description)))
            await node.add_property(self.idx, "EURange",
                                    ua.Range(Low=point.eu_low, High=point.eu_high))
            self._nodes[point.tag] = node
        for tag, _, description in DIGITALS:
            node = await rig.add_variable(self.idx, tag, False,
                                          ua.VariantType.Boolean)
            await node.set_writable(False)
            await node.add_property(self.idx, "Description", description)
            self._nodes[tag] = node
        log.info("bench rig address space: %d points", len(self._nodes))

    async def _connect(self) -> bool:
        from pymodbus.client import AsyncModbusTcpClient
        if self._client is not None and self._client.connected:
            return True
        self._client = AsyncModbusTcpClient(self.host, port=self.port)
        await self._client.connect()
        return bool(self._client.connected)

    async def _publish(self, tag: str, value, quality: int,
                       variant: ua.VariantType, source_ts: dt.datetime) -> None:
        node = self._nodes.get(tag)
        if node is None:
            return
        await node.write_value(ua.DataValue(
            Value=ua.Variant(value, variant),
            StatusCode=ua.StatusCode(quality),
            # The instant the bridge read the rig. The rig has no clock of its
            # own, so this is the earliest honest measurement time available,
            # and it is stated as such rather than pretending to be the
            # sensor's own.
            SourceTimestamp=source_ts))

    async def poll_once(self) -> bool:
        if not await self._connect():
            await self._mark_all_bad(BAD_NO_COMMUNICATION,
                                     "cannot reach the rig over Modbus")
            return False
        source_ts = dt.datetime.now(dt.timezone.utc)
        try:
            # pymodbus 3.15 names this device_id; it was `slave` in 3.x
            # before that, and `unit` before that again.
            registers = await self._client.read_input_registers(
                0, count=IREG_COUNT, device_id=self.unit_id)
            discretes = await self._client.read_discrete_inputs(
                0, count=len(DIGITALS), device_id=self.unit_id)
        except Exception as exc:
            self.failures += 1
            await self._mark_all_bad(BAD_NO_COMMUNICATION, str(exc))
            return False

        if registers.isError() or discretes.isError():
            self.failures += 1
            await self._mark_all_bad(BAD_NO_COMMUNICATION,
                                     f"Modbus exception: {registers}")
            return False

        words = list(registers.registers)
        status = words[IREG_STATUS]
        for point in POINTS:
            value, quality, reason = decode(point, words, status)
            if reason:
                log.warning("%s: %s", point.tag, reason)
            await self._publish(point.tag, value if value is not None else 0.0,
                                quality, ua.VariantType.Double, source_ts)
        for tag, bit, _ in DIGITALS:
            await self._publish(tag, bool(discretes.bits[bit]), GOOD,
                                ua.VariantType.Boolean, source_ts)
        self.polls += 1
        return True

    async def _mark_all_bad(self, quality: int, reason: str) -> None:
        """Losing the rig is not the same as the rig reading zero."""
        source_ts = dt.datetime.now(dt.timezone.utc)
        for point in POINTS:
            await self._publish(point.tag, 0.0, quality,
                                ua.VariantType.Double, source_ts)
        for tag, _, _ in DIGITALS:
            await self._publish(tag, False, quality, ua.VariantType.Boolean,
                                source_ts)

    async def run(self) -> None:
        log.info("bridging %s:%d unit %d every %.1fs", self.host, self.port,
                 self.unit_id, self.poll_interval_s)
        while True:
            try:
                await self.poll_once()
            except Exception as exc:
                log.exception("bridge poll failed: %s", exc)
            await asyncio.sleep(self.poll_interval_s)
