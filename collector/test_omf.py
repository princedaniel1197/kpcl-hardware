"""OMF message tests (Stage 9).

Validated against the AVEVA OMF 1.2 rules as published in
github.com/AVEVA/OMF-Docs. That repository publishes the specification as
documentation rather than as a JSON Schema file, so `validate` implements the
documented rules directly — stated plainly rather than glossed as "schema
validation".
"""

from __future__ import annotations

import datetime as dt

import pytest

from collector import omf
from collector.model import Sample

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
BAD = 2156593152


def sample(value, quality=0, name="U1_MW", n=0) -> Sample:
    return Sample(tag_id=1, tag_name=name,
                  source_ts=T0 + dt.timedelta(seconds=n),
                  server_ts=T0 + dt.timedelta(seconds=n, milliseconds=42),
                  value=value, quality=quality, seq=n + 1)


# --- the three message kinds validate ----------------------------------------

def test_the_type_message_validates():
    omf.validate("type", "create", omf.type_message())


def test_the_container_message_validates():
    containers = omf.container_message(
        [{"name": "U1_MW", "description": "gross", "engineering_unit": "MW"}])
    omf.validate("container", "create", containers)


def test_the_data_message_validates():
    omf.validate("data", "create", omf.data_message([sample(210.0)]))


def test_all_three_kinds_and_actions_are_accepted():
    for action in ("create", "update"):
        omf.validate("type", action, omf.type_message())
    omf.validate("type", "delete", [{"id": omf.SAMPLE_TYPE_ID}])


# --- the specification's rules are actually enforced -------------------------

def test_a_dynamic_type_without_an_index_is_rejected():
    """'At least one Type Property must be designated as the index, or the Type
    cannot be used to create instance data.'"""
    broken = omf.type_message()
    del broken[0]["properties"]["SourceTime"]["isindex"]
    with pytest.raises(omf.OmfError, match="isindex"):
        omf.validate("type", "create", broken)


def test_a_reserved_type_id_is_rejected():
    with pytest.raises(omf.OmfError, match="reserved"):
        omf.validate("type", "create",
                     [{"id": "__Link", "type": "object",
                       "classification": "dynamic", "properties": {}}])


def test_an_unsupported_format_is_rejected():
    broken = omf.type_message()
    broken[0]["properties"]["Value"]["format"] = "float128"
    with pytest.raises(omf.OmfError, match="format"):
        omf.validate("type", "create", broken)


def test_a_malformed_version_is_rejected():
    broken = omf.type_message()
    broken[0]["version"] = "1.0"
    with pytest.raises(omf.OmfError, match="x.x.x.x"):
        omf.validate("type", "create", broken)


def test_two_isname_properties_are_rejected():
    broken = omf.type_message()
    broken[1]["properties"]["Description"]["isname"] = True
    with pytest.raises(omf.OmfError, match="isname"):
        omf.validate("type", "create", broken)


def test_a_container_without_a_typeid_is_rejected():
    with pytest.raises(omf.OmfError, match="typeid"):
        omf.validate("container", "create", [{"id": "U1_MW"}])


def test_a_data_message_needs_a_container_type_or_properties():
    with pytest.raises(omf.OmfError, match="typeid, containerid or properties"):
        omf.validate("data", "create", [{"values": []}])


def test_an_empty_body_is_rejected():
    with pytest.raises(omf.OmfError, match="must not be empty"):
        omf.validate("data", "create", [])


# --- the rules this project cares about --------------------------------------

def test_the_index_is_the_source_timestamp_not_the_server_timestamp():
    """§335 expressed in the wire format: the historian orders and
    de-duplicates on when the value was produced."""
    properties = omf.type_message()[0]["properties"]
    assert properties["SourceTime"]["isindex"] is True
    assert "isindex" not in properties["ServerTime"]


def test_both_timestamps_are_sent_and_they_are_different():
    values = omf.data_message([sample(210.0)])[0]["values"][0]
    assert values["SourceTime"] != values["ServerTime"]
    assert values["SourceTime"] == "2026-10-01T00:00:00Z"
    assert values["ServerTime"] == "2026-10-01T00:00:00.042000Z"


def test_quality_is_a_designated_quality_property_and_an_integer():
    """OMF provides `isquality`, so the numeric StatusCode travels as a
    designated quality property rather than as an ordinary number."""
    quality = omf.type_message()[0]["properties"]["Quality"]
    assert quality["isquality"] is True
    assert quality["type"] == "integer"


def test_a_bad_sample_sends_an_explicit_null_value():
    """THE IMPORTANT ONE. The specification says an omitted property takes the
    type's default, and the default for `number` is 0. Expressing "no value" by
    omission would silently store a zero — the substitution §318 forbids,
    arriving through the wire format rather than through our code."""
    values = omf.data_message([sample(None, quality=BAD)])[0]["values"][0]
    assert "Value" in values, "Value must be present, or OMF defaults it to 0"
    assert values["Value"] is None
    assert values["Quality"] == BAD


def test_quality_is_never_a_boolean_or_a_string():
    values = omf.data_message([sample(1.0, quality=BAD)])[0]["values"][0]
    assert isinstance(values["Quality"], int)
    assert not isinstance(values["Quality"], bool)


def test_naive_timestamps_are_refused():
    naive = Sample(1, "U1_MW", dt.datetime(2026, 10, 1),
                   dt.datetime(2026, 10, 1), 1.0, 0, 1)
    with pytest.raises(omf.OmfError, match="timezone-aware"):
        omf.data_message([naive])


def test_samples_are_bulked_by_container():
    """'To achieve optimal throughput, bulking and compression of messages is
    recommended.'"""
    samples = [sample(1.0, name="U1_MW", n=0), sample(2.0, name="U1_MW", n=1),
               sample(3.0, name="U1_MS_TEMP", n=2)]
    message = omf.data_message(samples)
    by_container = {m["containerid"]: len(m["values"]) for m in message}
    assert by_container == {"U1_MW": 2, "U1_MS_TEMP": 1}


def test_engineering_units_travel_as_a_container_property_override():
    container = omf.container_message(
        [{"name": "U1_MW", "engineering_unit": "MW"}])[0]
    assert container["propertyoverrides"]["Value"]["uom"] == "MW"


# --- headers ------------------------------------------------------------------

def test_headers_carry_the_message_kind_action_and_version():
    client = omf.OmfClient(omf.OmfConfig(url="http://example/omf",
                                         producer_token="tok"))
    headers = client.headers("data", "create")
    assert headers["messageformat"] == "JSON"
    assert headers["omfversion"] == "1.2"
    assert headers["messagetype"] == "data"
    assert headers["action"] == "create"
    assert headers["producertoken"] == "tok"


def test_credentials_are_configuration_not_code():
    """Pointing at a real PI Web API OMF endpoint is a URL and credentials."""
    client = omf.OmfClient(omf.OmfConfig(
        url="https://pi.example/piwebapi/omf", producer_token="tok",
        username="user", password="secret"))
    assert client.headers("data", "create")["Authorization"].startswith("Basic ")
