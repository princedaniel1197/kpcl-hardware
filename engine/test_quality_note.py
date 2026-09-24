"""A tag with a standing quality note is shown, never used.

RIG_CURRENT carries "uncalibrated - bench demo only" (24 September 2026): it
read 0.12-0.88 A for fans a meter put at 0.1-0.25 A, and the hardware is being
left as it is. It is published Uncertain and displayed with the note. What must
not happen is a KPI, an alert or an event frame quietly building on it -- an
Uncertain input would only make a KPI Uncertain, which is true but not enough:
a number derived from a reading known to be wrong by a factor of four has no
business on the KPI strip at all.

These read the configuration, which is what the engine and the seeders load,
so adding such a dependency fails here before it reaches the archive.

Would fail if: a KPI input, an event-template trigger or an alert subject
resolves to a tag whose config carries a quality_note.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CONFIG = Path(__file__).parent.parent / "config"


def _load(name: str) -> dict:
    return json.loads((CONFIG / name).read_text())


def noted_tags() -> set[str]:
    return {t["name"] for t in _load("unit1_tags.json")["tags"] if t.get("quality_note")}


def attributes_of(tags: set[str]) -> set[str]:
    """Attribute names in the asset model whose tag pattern yields one of `tags`."""
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            if "tag_pattern" in node and "name" in node:
                pattern = re.escape(node["tag_pattern"]).replace(r"\{unit\}", ".+")
                if any(re.fullmatch(pattern, tag) for tag in tags):
                    found.add(node["name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(_load("asset_model.json"))
    return found


def test_rig_current_carries_the_note():
    assert "RIG_CURRENT" in noted_tags()
    assert attributes_of({"RIG_CURRENT"}) == {"LoadCurrent"}


def test_no_kpi_uses_a_noted_tag():
    banned = attributes_of(noted_tags())
    for d in _load("kpi_definitions.json")["definitions"]:
        used = set(d["inputs"].values()) & banned
        assert not used, f"KPI {d['name']} uses {used}, which carry a quality_note"


def test_no_event_template_uses_a_noted_tag():
    banned = attributes_of(noted_tags())
    for t in _load("event_templates.json")["templates"]:
        triggers = [t["start"], t["end"]] + [m["trigger"] for m in t.get("milestones", [])]
        used = {tr["attribute"] for tr in triggers} & banned
        assert not used, f"event template {t['name']} uses {used}"


def test_no_alert_uses_a_noted_tag():
    noted = noted_tags()
    for r in _load("alert_rules.json")["rules"]:
        if r["subject_kind"] == "tag":
            assert r["subject"] not in noted, f"alert {r['name']} is on {r['subject']}"
        # A KPI subject is covered by test_no_kpi_uses_a_noted_tag.


def test_the_rule_can_fail():
    """The check is not vacuous: a KPI on LoadCurrent is caught."""
    banned = attributes_of(noted_tags())
    fake = {"name": "RigPower", "inputs": {"i": "LoadCurrent", "v": "SupplyVoltage"}}
    assert set(fake["inputs"].values()) & banned == {"LoadCurrent"}
