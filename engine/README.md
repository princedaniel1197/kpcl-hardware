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
<unit>_<measurement>          U1_MS_TEMP, U2_BEARING_VIB
COLLECTOR_<measurement>       COLLECTOR_BUFFER_DEPTH
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
 "context": {"station": "RTPS", "unit": "U2"}}
```

```bash
make seed-assets
```

That creates five elements, sixteen attributes, and **the tag rows those
attributes resolve to**, carrying units, span, scan rate and deadbands from the
attribute templates. The last part is what makes the rule true rather than
decorative: if instantiating a unit left its tags to be written by hand, adding
a unit would still be a manual remapping job wearing a template's clothes.

Every element and tag created is written to `audit_log` with actor and reason
(§433).

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
