"""Modbus-to-OPC UA bridge for the bench rig. (Stage 10)

Reads the ESP32 over Modbus TCP with pymodbus and republishes into the Stage 1
OPC UA server's address space as a second unit, so that **the collector requires
no change**: it already subscribes to whatever tags the tag table lists, and the
rig's tags were created from a template by Stage 5.

THE MAPPING THAT MATTERS. The firmware's status word says, per sensor, whether
this scan's reading can be trusted. A clear bit becomes a Bad StatusCode on that
tag and nothing else -- the value is published as null, the previous value is
not republished, and no zero is invented. That is §318 crossing a fieldbus
boundary, which is the one place it is easiest to lose. The Bad code says which
failure it was: BadDeviceFailure for a sensor that did not read (the build
plan's mapping), BadConfigurationError for temperature probes whose ROM
addresses have not been configured in the firmware, BadOutOfRange for a
supply voltage outside what the ADC can measure, and BadNotConnected for a
digital input whose hardware is not fitted.

The register map is REGISTER_MAP.md, version 3 (bench bring-up, 24 September
2026; version 2 came from the review of 23 September): current is signed so
that a wrong zero shows as negative instead of being hidden, every channel has
a sentinel, the supply status bit means "the ADC could measure it" -- at either
end of its range -- rather than "the supply is within limits", and status bits
8 and 9 say whether the run switch and the relays are fitted at all. A digital
input whose hardware is absent reads a plain False, which published as Good
would say "stopped" about a rig that is running.

The bridge lives in sim/ rather than collector/ because it is a source of data,
not an acquirer of it. It WRITES values into the OPC UA server's address space --
that is what republishing is -- and putting it in collector/ would put an OPC UA
write inside the package that is required to have no write path at all. It
issues no Modbus write to the rig; a test asserts that.
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
IREG_ACS_ZERO_MV = 7
IREG_COUNT = 8

ST_HUB_OK = 1 << 0
ST_AMBIENT_OK = 1 << 1
ST_MPU_OK = 1 << 2
ST_ACS_OK = 1 << 3
ST_SUPPLY_OK = 1 << 4
ST_BUS_OK = 1 << 5
ST_ACS_ZEROED = 1 << 6
ST_PROBES_CONFIG = 1 << 7
ST_SWITCH_FITTED = 1 << 8
ST_RELAYS_FITTED = 1 << 9

# Sentinels no sensor on the rig can produce.
INVALID_S16 = -32768
INVALID_U16 = 0xFFFF
TEMP_INVALID = INVALID_S16          # the name the stand-in and tests use

GOOD = int(ua.StatusCodes.Good)
BAD_DEVICE_FAILURE = int(ua.StatusCodes.BadDeviceFailure)
BAD_CONFIGURATION = int(ua.StatusCodes.BadConfigurationError)
BAD_OUT_OF_RANGE = int(ua.StatusCodes.BadOutOfRange)
BAD_NO_COMMUNICATION = int(ua.StatusCodes.BadNoCommunication)
BAD_NOT_CONNECTED = int(ua.StatusCodes.BadNotConnected)


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
    # Which Bad code a clear status bit means for this point.
    bad_code: int = BAD_DEVICE_FAILURE
    needs_probe_config: bool = False
    # What a clear status bit means, in words, for the log.
    clear_reason: str = "the sensor was not read successfully this scan"

    @property
    def invalid(self) -> int:
        return INVALID_S16 if self.signed else INVALID_U16


POINTS: tuple[Point, ...] = (
    # The ACS712-05B spans -5 A to +5 A; that is the instrument's range.
    Point("RIG_CURRENT", IREG_CURRENT_MA, 0.001, "A",
          "Bench rig total load current (ACS712 5 A)", ST_ACS_OK, signed=True,
          eu_low=-5.0, eu_high=5.0),
    Point("RIG_VIBRATION", IREG_VIB_MMS_X100, 0.01, "mm/s",
          "Bench rig fan vibration (MPU-6050 or MPU-6500, approximate velocity)",
          ST_MPU_OK, eu_low=0.0, eu_high=50.0),
    Point("RIG_HUB_TEMP", IREG_TEMP_HUB_X10, 0.1, "degC",
          "Fan motor hub temperature (DS18B20)", ST_HUB_OK, signed=True,
          eu_low=-55.0, eu_high=125.0, needs_probe_config=True),
    Point("RIG_AMBIENT_TEMP", IREG_TEMP_AMB_X10, 0.1, "degC",
          "Ambient temperature (DS18B20)", ST_AMBIENT_OK, signed=True,
          eu_low=-55.0, eu_high=125.0, needs_probe_config=True),
    Point("RIG_SUPPLY_V", IREG_SUPPLY_MV, 0.001, "V",
          "Bench rig supply voltage", ST_SUPPLY_OK, eu_low=0.0, eu_high=15.0,
          bad_code=BAD_OUT_OF_RANGE,
          clear_reason="the supply is outside what the ADC can measure: below "
                       "its floor (about 0.75 V, e.g. 12 V disconnected) or at "
                       "its ceiling"),
)

# The discrete inputs report what the firmware COMMANDED the relays to do; the
# rig has no contact feedback, and the names say so. The last field is the
# status bit that says the hardware behind the input is fitted.
DIGITALS = (("RIG_RUNNING", 0, "Rig run/stop state (rocker switch)", ST_SWITCH_FITTED),
            ("RIG_RELAY_1", 1, "Relay group 1 commanded on", ST_RELAYS_FITTED),
            ("RIG_RELAY_2", 2, "Relay group 2 commanded on", ST_RELAYS_FITTED))


def _signed(word: int) -> int:
    return word - 0x10000 if word >= 0x8000 else word


def decode(point: Point, registers: list[int], status: int
           ) -> tuple[float | None, int, str | None]:
    """Turn one raw register into a value and a StatusCode.

    Returns (value, quality, reason). A value is returned only when the
    firmware says the sensor was read successfully this scan. Otherwise the
    value is None — not the last one, not zero.
    """
    if point.needs_probe_config and not (status & ST_PROBES_CONFIG):
        return None, BAD_CONFIGURATION, (
            f"{point.tag}: the DS18B20 ROM addresses are not configured in the "
            f"firmware (rig_config.h), so no probe can be trusted to be this one")
    if point.status_bit == ST_ACS_OK and not (status & ST_ACS_ZEROED):
        return None, BAD_DEVICE_FAILURE, (
            f"{point.tag}: the ACS712 has not been zeroed with the load off -- "
            f"12 V was already on at boot with no relays to switch the fans, or "
            f"the zero was implausible -- so no current can be trusted")
    if not (status & point.status_bit):
        return None, point.bad_code, (
            f"firmware status bit for {point.tag} is clear: {point.clear_reason}")
    raw = registers[point.register]
    if raw == (point.invalid & 0xFFFF):
        # Belt and braces: the status bit should already have caught this.
        # If it did not, the firmware and the bridge disagree, which is
        # worth saying rather than silently trusting one of them.
        return None, BAD_DEVICE_FAILURE, (
            f"{point.tag} holds the invalid sentinel although its status "
            f"bit is set — firmware and bridge disagree")
    if point.signed:
        raw = _signed(raw)
    return raw * point.scale, GOOD, None


def decode_digital(tag: str, bit: int, fitted_bit: int, bits: list[bool],
                   status: int) -> tuple[bool | None, int, str | None]:
    """A discrete input, unless the hardware behind it is not fitted -- then
    Bad with no value, because an absent switch reads a plain False."""
    if not (status & fitted_bit):
        return None, BAD_NOT_CONNECTED, (
            f"{tag}: not fitted on this rig (rig_config.h), so the input "
            f"reads nothing and its False means nothing")
    return bool(bits[bit]), GOOD, None


class ModbusBridge:
    """Polls the rig and publishes into an existing OPC UA server.

    Over Modbus TCP (the rig on WiFi) or Modbus RTU (the rig on RS-485 through
    a MAX485, read from the USB-CH340 adapter), with the same register map and
    the same decoding either way.
    """

    def __init__(self, server, namespace_index: int, host: str | None = None,
                 port: int = 502, unit_id: int = 1,
                 poll_interval_s: float = 1.0, *,
                 serial_port: str | None = None, baudrate: int = 19200) -> None:
        if (host is None) == (serial_port is None):
            raise ValueError("give either a TCP host or a serial port")
        self.server = server
        self.idx = namespace_index
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.unit_id = unit_id
        self.poll_interval_s = poll_interval_s
        self._nodes: dict[str, object] = {}
        self._client = None
        self.polls = 0
        self.failures = 0
        self._last_failure: str | None = None
        self._reasons: dict[str, str | None] = {}

    @property
    def where(self) -> str:
        if self.serial_port:
            return f"{self.serial_port} at {self.baudrate} baud 8N1 (RTU)"
        return f"{self.host}:{self.port} (TCP)"

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
        for tag, _, description, _ in DIGITALS:
            node = await rig.add_variable(self.idx, tag, False,
                                          ua.VariantType.Boolean)
            await node.set_writable(False)
            await node.add_property(self.idx, "Description", description)
            self._nodes[tag] = node
        log.info("bench rig address space: %d points", len(self._nodes))

    async def _connect(self) -> bool:
        if self._client is not None and self._client.connected:
            return True
        if self.serial_port:
            from pymodbus.client import AsyncModbusSerialClient
            self._client = AsyncModbusSerialClient(
                self.serial_port, baudrate=self.baudrate, bytesize=8,
                parity="N", stopbits=1)
        else:
            from pymodbus.client import AsyncModbusTcpClient
            self._client = AsyncModbusTcpClient(self.host, port=self.port)
        await self._client.connect()
        return bool(self._client.connected)

    async def _publish(self, tag: str, value, quality: int,
                       variant: ua.VariantType, source_ts: dt.datetime) -> None:
        """Publish into the address space. A value of None is published as a
        null Variant: not zero, not the previous value."""
        node = self._nodes.get(tag)
        if node is None:
            return
        await node.write_value(ua.DataValue(
            Value=(ua.Variant(None, ua.VariantType.Null) if value is None
                   else ua.Variant(value, variant)),
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

        self._last_failure = None
        words = list(registers.registers)
        status = words[IREG_STATUS]
        for point in POINTS:
            value, quality, reason = decode(point, words, status)
            self._log_reason(point.tag, reason)
            await self._publish(point.tag, value, quality,
                                ua.VariantType.Double, source_ts)
        for tag, bit, _, fitted_bit in DIGITALS:
            value, quality, reason = decode_digital(tag, bit, fitted_bit,
                                                    discretes.bits, status)
            self._log_reason(tag, reason)
            await self._publish(tag, value, quality,
                                ua.VariantType.Boolean, source_ts)
        self.polls += 1
        return True

    def _log_reason(self, tag: str, reason: str | None) -> None:
        """Logged when it changes, not on every poll."""
        if reason != self._reasons.get(tag):
            if reason:
                log.warning("%s: %s", tag, reason)
            elif tag in self._reasons:
                log.info("%s: reading again", tag)
            self._reasons[tag] = reason

    async def _mark_all_bad(self, quality: int, reason: str) -> None:
        """Losing the rig is not the same as the rig reading zero."""
        if reason != self._last_failure:
            log.warning("rig unavailable: %s", reason)
            self._last_failure = reason
        source_ts = dt.datetime.now(dt.timezone.utc)
        for point in POINTS:
            await self._publish(point.tag, None, quality,
                                ua.VariantType.Double, source_ts)
        for tag, _, _, _ in DIGITALS:
            await self._publish(tag, None, quality, ua.VariantType.Boolean,
                                source_ts)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def run(self) -> None:
        log.info("bridging %s unit %d every %.1fs", self.where, self.unit_id,
                 self.poll_interval_s)
        while True:
            try:
                await self.poll_once()
            except Exception as exc:
                log.exception("bridge poll failed: %s", exc)
            await asyncio.sleep(self.poll_interval_s)
