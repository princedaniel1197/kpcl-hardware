# engine — asset model, quality, KPIs, event frames

## Naming convention (§385)

Tags and elements follow ISA-95 and ISA-88 principles. The convention is
documented here and is not changed ad hoc.

### Element hierarchy (ISA-95 equipment hierarchy, §392)

```
Enterprise  KPCL
  Station     KPCL-RTPS
    Unit        KPCL-RTPS-U1
      System      KPCL-RTPS-U1-BLR   (Boiler)
                  KPCL-RTPS-U1-TUR   (Turbine)
                  KPCL-RTPS-U1-GEN   (Generator)
        Equipment   KPCL-RTPS-U1-TUR-BRG1
          Component   ...
            Parameter   ...
```

The **asset code** is the path, hyphen-separated, uppercase, most significant
part first. It is unique and **permanent: a code is never reused** (§392).
Reusing one would silently re-point a unit's history at a different physical
thing, so `instantiate()` refuses a code that already exists.

The code is deliberately not the primary key. The surrogate `element.id` may
change under a migration; the asset code may not.

Levels, coarse to fine: `Enterprise, Station, Unit, System, SubSystem,
Equipment, Component, Parameter`. Depth is data — `element.parent_id` is
self-referencing — so a level can be inserted without a schema change.

### Tag names

```
<unit>_<measurement>                    U1_MS_TEMP, U2_BEARING_VIB
COLLECTOR_<INSTANCE>_<measurement>      COLLECTOR_PRIMARY_BUFFER_DEPTH
RIG_<measurement>                       RIG_HUB_TEMP
```

Uppercase, underscore-separated, unit prefix first. The prefix is what makes a
tag name resolvable from a template: an attribute template holds the *pattern*
(`{unit}_MS_TEMP`) and an element holds the *context* (`{"unit": "U2"}`).

A pattern naming something the context does not carry is an error, not an empty
string. Producing `_MS_TEMP` would create an attribute pointing at a tag that
will never exist, and nothing downstream would say so.

### Attribute names

`PascalCase`, describing the measurement rather than the instrument:
`MainSteamTemperature`, not `TE-4021`. The instrument tag is what the attribute
*resolves to*; the attribute is what an engineer asks for.

## Templates: derivation and composition

Two different relationships, and conflating them is a common mistake.

**Derivation** (`element_template.parent_template_id`) is *is-a*.
`ThermalUnit210` derives from `GeneratingUnit`: it inherits every attribute and
restates only what differs — `RatedCapacity` 210, `Fuel` coal.

When a derived template restates an attribute, **the derived value wins**. That
requires ordering the derivation chain by depth; without it the winner is
arbitrary, and `Fuel` came out `unknown` from the base rather than `coal`.

**Composition** (`template_composition`) is *has-a*. A `ThermalUnit210`
contains a `Boiler`, a `Turbine` and a `Generator`; a `Turbine` contains a
`Bearing`. Instantiating a unit creates the whole subtree.

## Adding an asset is configuration, not code (rule 7)

Adding Unit 2 is one object in `config/asset_model.json`:

```json
{"template": "ThermalUnit210", "asset_code": "KPCL-RTPS-U2",
 "name": "RTPS Unit 2", "parent_code": "KPCL-RTPS",
 "context": {"station": "RTPS", "unit": "U2", "source_path": "Unit2"}}
```

`source_path` says where the unit's tags live in the source's address space.
Without it the tags have no source path and the collector refuses them, loudly,
rather than guessing: until 23 September a column default guessed `Unit1`, and
Unit 2's fourteen tags were filed there. (The simulator models one unit, so
Unit 2's tags are counted as unresolved and its KPIs are Bad, naming inputs with
no value — which is the truth about a unit with no source.)

```bash
make seed-assets
```

That creates five elements, sixteen attributes, and **the tag rows those
attributes resolve to**, carrying units, span, scan rate and deadbands from the
attribute templates. The last part is what makes the rule true rather than
decorative: if instantiating a unit left its tags to be written by hand, adding
a unit would still be a manual remapping job wearing a template's clothes.

Every element and tag created, and every tag mapped to an element, is written to
`audit_log` with actor and reason (§433).

## The query an asset framework exists for

```python
assets.attribute_across_template(conn, "Bearing", "Vibration")
assets.latest_across_template(conn, "Boiler", "MainSteamTemperature")
```

Every bearing vibration on every unit; every main steam temperature in the
fleet. Without it, "compare this across the fleet" is a hand-written query per
unit, which is the state an asset framework exists to replace.

It spans **derived** templates: asking `GeneratingUnit` finds the 210 MW units
and would find 500 MW ones beside them. Matching the template name exactly
returns nothing at all for a base template — which is the query a mixed fleet
most wants to ask.

`latest_across_template` carries quality with each value. A fleet comparison
that cannot say which units are reporting Bad is not a comparison.

## Quality rules and tag health (Stage 6)

```bash
make seed-quality    # thresholds from config/quality_rules.json
make quality-demo    # the Stage 6 acceptance test, on the live system
```

**Source quality is never overwritten.** `sample.quality` holds the StatusCode
exactly as acquired, for ever. Computed verdicts live in `quality_flag`, and the
`sample_quality` view shows both columns side by side. A value that arrived Good
but failed a range check is not the same thing as a value that arrived Bad — the
first is a measurement the instrument stands behind and the system doubts, the
second one the instrument disowns.

Severity is assigned on meaning, not convenience:

| Rule | Verdict | Why |
|---|---|---|
| range | `BadOutOfRange` | outside what the instrument can represent — not a measurement at all |
| stale | `BadNoCommunication` | nothing arriving; there is no measurement to judge |
| rate of change | `Uncertain` | the reading may be real; what is doubted is whether to believe it. OPC UA has no specific code for implausible rate, and the one used before this review (`UncertainSensorNotAccurate`) claims the value is at a sensor limit, which the rule does not know |
| cross-tag | `UncertainSubNormal` | two instruments disagree; which is wrong is not yet known |
| frozen | `UncertainLastUsableValue` | a steady process and a stuck transmitter look alike |

### Two findings worth carrying forward

**A server-side deadband hid every quality change.** asyncua 2.0.1's server ANDs
the data-change trigger with the deadband test, so a status change with the
value unchanged is never sent. Forcing a tag Bad through a deadband subscription
produced zero Bad notifications. For that server the collector applies **no
deadband at the source** — a per-server setting since 23 September
(`config/sources.json`), on by default for any other server. The cost, measured
rather than assumed (§317): 2.2× the notifications during a start-up (10.0/s →
22.2/s over 14 tags, 22 September), 3.2× at steady load (6.8/s → 22.0/s, 23
September).

**A stuck transmitter produces silence, not repetition.** A subscription reports
on change, so with subscription alone "frozen" and "stale" are the same
observable — and §439 needs them distinguished, because a tag that is alive but
stuck is a different fault from one whose link has failed. The collector
therefore performs a periodic **max-time read** of any tag that has gone quiet.
It is a read, the session stays read-only, and the returned DataValue carries
the server's own SourceTimestamp; nothing is re-stamped.

### Configuration changes made to force a condition are audited

`make quality-demo` narrows an EURange and tightens a tolerance to force two of
the conditions. It does so through `archive/audit.py`, like any configuration
change, and restores in a `finally`. The first version used direct UPDATEs, and
the 1,251 range flags it left behind had nothing in `audit_log` to explain them.

### The cross-tag pair

Feedwater flow against gross generation, ~3.19 t/h per MW, **gated on load**.
The build plan suggests feedwater against main steam flow, which the simulator
does not have. The rule is gated because the relationship holds in a regime, not
always: during a cold start-up the boiler is filled with the breaker open, and
an ungated rule would fire through every start-up. A check that cries wolf
through every normal evolution gets switched off.

## The engine service

```bash
make engine        # python -m engine
```

Nothing above computes anything on its own. Until 23 September KPIs, quality
verdicts and event frames were produced only while a demonstration script ran,
so the dashboard showed whatever the last one had left. `engine/service.py`
runs them continuously:

| | How often | Over |
|---|---|---|
| every current KPI definition | its own `calculation_freq_ms` | every element it applies to |
| every quality rule | every 10 s | every acquired tag, last 5 minutes |
| event-frame detection | every 30 s | the last template duration on the first pass, then incrementally; idempotent on (element, template, start) |

**Which elements a KPI or an event template applies to is derived** from the
asset model: the lowest elements whose subtree holds every attribute it needs.
Heat rate needs CoalFlow (on the boiler) and GrossGeneration (on the unit), so it
applies to each Unit; the Station holds both only through a Unit. Adding a unit
is still configuration.

When the archive is unreachable the engine waits and resumes. It computes KPIs
for now and does not go back to compute them for the period the archive was down
— recomputing history from replayed data is a deliberate act, not a side effect.
