"""OMF (OSIsoft Message Format) output. (Stage 9)

Follows the AVEVA OMF 1.2 specification, github.com/AVEVA/OMF-Docs, Apache-2.0.
The three message kinds are Type, Container and Data, posted as JSON over HTTP
with the message kind and action in the headers.

TWO THINGS THE SPECIFICATION MADE POSSIBLE, AND ONE IT MADE DANGEROUS.

**SourceTime is the index.** A dynamic OMF type must designate one property as
`isindex`, and that property is what the historian orders and de-duplicates on.
Making it the source timestamp rather than the server timestamp carries rule 1
(§335) all the way into PI: what gets archived is indexed by when the value was
produced. ServerTime rides alongside as an ordinary property, so the transit is
still visible and still separate.

**Quality is first-class in OMF.** The specification provides `isquality` on a
type property: "one or more Type Properties may optionally be designated as
quality... these properties would then determine the overall quality of each data
value." So the numeric OPC UA StatusCode travels as a designated quality
property rather than being smuggled through as an ordinary number.

**And the hazard.** From the Data message specification: "If a property is
defined on the Type definition, and that property is not included in the values
array, then a default value for that property will be assumed." The default for
`number` is **0**. So omitting Value for a Bad sample — the obvious way to
express "there is no value" — would silently store a zero. That is precisely the
substitution §318 forbids, arriving through the wire format rather than through
our code. Value is therefore always present and explicitly `null` when the
sample carries none, and a test asserts it.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from collector.model import Sample

OMF_VERSION = "1.2"
SAMPLE_TYPE_ID = "CRPMS.Sample"
TAG_TYPE_ID = "CRPMS.Tag"
DATASOURCE = "orianode-crpms"

VALID_MESSAGE_TYPES = ("type", "container", "data")
VALID_ACTIONS = ("create", "update", "delete")
VALID_CLASSIFICATIONS = ("dynamic", "static")

# From the specification's Supported Formats table.
SUPPORTED = {
    "boolean": {None},
    "integer": {None, "int64", "int32", "int16", "uint64", "uint32", "uint16"},
    "number": {None, "float64", "float32", "float16"},
    "string": {None, "date-time"},
    "object": {None},
    "array": {None},
}


class OmfError(RuntimeError):
    pass


# -- message construction -----------------------------------------------------

def type_message() -> list[dict]:
    """The Type message: one dynamic type for samples, one static for tags."""
    return [
        {
            "id": SAMPLE_TYPE_ID,
            "version": "1.0.0.0",
            "type": "object",
            "classification": "dynamic",
            "name": "CRPMS time series sample",
            "description": ("A measurement carrying its source timestamp, the "
                            "server timestamp, and the numeric OPC UA "
                            "StatusCode"),
            "properties": {
                # The index is the SOURCE timestamp. This is §335 expressed in
                # the wire format: the historian orders and de-duplicates on
                # when the value was produced, not when it arrived.
                "SourceTime": {
                    "type": "string", "format": "date-time", "isindex": True,
                    "name": "Source timestamp",
                    "description": "When the value was produced",
                },
                "ServerTime": {
                    "type": "string", "format": "date-time",
                    "name": "Server timestamp",
                    "description": ("When the value went on the wire; null "
                                    "when no server stamped it"),
                },
                "Value": {
                    "type": "number", "format": "float64",
                    "name": "Value",
                    "description": "Null when the sample carries no value",
                },
                "Quality": {
                    "type": "integer", "format": "int64", "isquality": True,
                    "name": "OPC UA StatusCode",
                    "description": "Numeric StatusCode as acquired, never a "
                                   "boolean or a string",
                },
            },
        },
        {
            "id": TAG_TYPE_ID,
            "version": "1.0.0.0",
            "type": "object",
            "classification": "static",
            "name": "CRPMS tag",
            "properties": {
                "TagName": {"type": "string", "isindex": True, "isname": True},
                "Description": {"type": "string"},
                "EngineeringUnit": {"type": "string"},
                "RangeLow": {"type": "number", "format": "float64"},
                "RangeHigh": {"type": "number", "format": "float64"},
                "AssetCode": {"type": "string"},
            },
        },
    ]


def container_message(tags: list[dict]) -> list[dict]:
    """One container per tag, all of the dynamic sample type."""
    containers = []
    for tag in tags:
        container = {
            "id": tag["name"],
            "typeid": SAMPLE_TYPE_ID,
            "name": tag["name"],
            "datasource": DATASOURCE,
        }
        if tag.get("description"):
            container["description"] = tag["description"]
        overrides = {}
        if tag.get("engineering_unit"):
            # `uom` is one of the overrides the specification permits on a
            # container, so engineering units travel with the stream.
            overrides["Value"] = {"uom": tag["engineering_unit"]}
        if overrides:
            container["propertyoverrides"] = overrides
        containers.append(container)
    return containers


def data_message(samples: list[Sample]) -> list[dict]:
    """Data grouped by container, as the specification recommends bulking."""
    by_container: dict[str, list[dict]] = {}
    for s in samples:
        by_container.setdefault(s.tag_name, []).append({
            "SourceTime": _iso(s.source_ts),
            # Explicitly null when no server stamped the value. Never filled
            # from SourceTime, and never omitted -- an omitted property takes
            # the type's default.
            "ServerTime": _iso(s.server_ts) if s.server_ts is not None else None,
            # ALWAYS present. Omitting it would let the endpoint substitute the
            # type's default of 0 for a Bad sample.
            "Value": s.value,
            "Quality": int(s.quality),
        })
    return [{"containerid": cid, "values": values}
            for cid, values in by_container.items()]


def _iso(when: dt.datetime) -> str:
    if when.tzinfo is None:
        raise OmfError("timestamps sent as OMF must be timezone-aware")
    return when.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


# -- validation ---------------------------------------------------------------

def validate(message_type: str, action: str, body: list) -> None:
    """Validate a message against the OMF 1.2 rules.

    The AVEVA repository publishes the specification as documentation rather
    than as a JSON Schema document, so this implements the documented rules
    directly. That is stated rather than glossed: this is a structural
    validator written from the specification, not the specification's own
    schema file, because no such file is published.
    """
    if message_type not in VALID_MESSAGE_TYPES:
        raise OmfError(f"messagetype must be one of {VALID_MESSAGE_TYPES}")
    if action not in VALID_ACTIONS:
        raise OmfError(f"action must be one of {VALID_ACTIONS}")
    if not isinstance(body, list):
        raise OmfError("an OMF message body is a JSON array")
    if not body:
        raise OmfError("an OMF message body must not be empty")

    if message_type == "type":
        for item in body:
            _validate_type(item, action)
    elif message_type == "container":
        for item in body:
            _require(item, "id", str)
            if action != "delete":
                _require(item, "typeid", str)
    else:
        for item in body:
            if not ({"typeid", "containerid", "properties"} & set(item)):
                raise OmfError(
                    "a data message needs typeid, containerid or properties")
            if "values" in item and not isinstance(item["values"], list):
                raise OmfError("values must be an array")


def _validate_type(item: dict, action: str) -> None:
    _require(item, "id", str)
    if item["id"].startswith("__"):
        raise OmfError("type ids beginning with __ are reserved")
    if action == "delete":
        return
    classification = item.get("classification")
    if classification is not None and classification not in VALID_CLASSIFICATIONS:
        raise OmfError(f"classification must be one of {VALID_CLASSIFICATIONS}")
    if "version" in item:
        parts = str(item["version"]).split(".")
        if len(parts) != 4 or not all(p.isdigit() for p in parts):
            raise OmfError("version must be x.x.x.x with non-negative integers")
    if "enum" not in item:
        _require(item, "properties", dict)
        if item.get("type") != "object":
            raise OmfError("a static or dynamic type must have type 'object'")
        indexes = [n for n, p in item["properties"].items() if p.get("isindex")]
        if classification in VALID_CLASSIFICATIONS and not indexes:
            raise OmfError(
                f"type {item['id']}: at least one property must be isindex, or "
                "the type cannot be used to create instance data")
        names = [n for n, p in item["properties"].items() if p.get("isname")]
        if len(names) > 1:
            raise OmfError("at most one property may be isname")
        for name, prop in item["properties"].items():
            if "type" not in prop and "reftypeid" not in prop:
                raise OmfError(f"property {name}: type or reftypeid is required")
            if "type" in prop:
                kind = prop["type"]
                if kind not in SUPPORTED:
                    raise OmfError(f"property {name}: unsupported type {kind!r}")
                fmt = prop.get("format")
                if fmt not in SUPPORTED[kind]:
                    raise OmfError(
                        f"property {name}: format {fmt!r} is not valid for "
                        f"type {kind!r}")


def _require(item: dict, key: str, kind: type) -> None:
    if key not in item:
        raise OmfError(f"missing required keyword {key!r}")
    if not isinstance(item[key], kind):
        raise OmfError(f"{key!r} must be {kind.__name__}")


# -- sending ------------------------------------------------------------------

@dataclass(frozen=True)
class OmfConfig:
    """Everything needed to point at an endpoint.

    Pointing the collector at a real PI Web API OMF endpoint is a URL and a
    token here. No code changes.
    """
    url: str
    producer_token: str = "orianode-crpms"
    verify_tls: bool = True
    timeout_s: float = 20.0
    compress: bool = False
    # PI Web API wants Basic auth in addition to the producer token.
    username: str | None = None
    password: str | None = None


class OmfClient:
    """Posts OMF messages over HTTP."""

    def __init__(self, config: OmfConfig) -> None:
        self.config = config
        self.sent = {"type": 0, "container": 0, "data": 0}

    def headers(self, message_type: str, action: str) -> dict[str, str]:
        headers = {
            "messageformat": "JSON",
            "omfversion": OMF_VERSION,
            "messagetype": message_type,
            "action": action,
            "producertoken": self.config.producer_token,
            "Content-Type": "application/json",
        }
        if self.config.compress:
            headers["compression"] = "gzip"
        if self.config.username is not None:
            import base64
            raw = f"{self.config.username}:{self.config.password or ''}"
            headers["Authorization"] = (
                "Basic " + base64.b64encode(raw.encode()).decode())
        return headers

    def post(self, message_type: str, action: str, body: list) -> int:
        validate(message_type, action, body)
        payload = json.dumps(body).encode()
        if self.config.compress:
            payload = gzip.compress(payload)
        request = urllib.request.Request(
            self.config.url, data=payload, method="POST",
            headers=self.headers(message_type, action))
        try:
            with urllib.request.urlopen(request,
                                        timeout=self.config.timeout_s) as r:
                self.sent[message_type] += 1
                return r.status
        except urllib.error.HTTPError as exc:
            raise OmfError(
                f"OMF endpoint returned {exc.code}: "
                f"{exc.read()[:300].decode(errors='replace')}") from exc
        except Exception as exc:
            raise OmfError(f"OMF post failed: {exc}") from exc
