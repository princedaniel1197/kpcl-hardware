# Stage 5 — Asset framework

| | |
|---|---|
| Stage | 5 — Asset framework |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 5 |
| Acceptance criterion | Adding a second simulated unit requires creating one element from a template, not re-mapping tags by hand. |
| Date performed | 2026-09-22 |
| **Result** | **PASS** |

## The criterion, demonstrated on the live database

Adding RTPS Unit 2 was **one object appended to `config/asset_model.json`**:

```json
{"template": "ThermalUnit210", "asset_code": "KPCL-RTPS-U2",
 "name": "RTPS Unit 2", "parent_code": "KPCL-RTPS",
 "context": {"station": "RTPS", "unit": "U2"}}
```

then `make seed-assets`. No Python was edited. No tag was remapped.

What appeared:

| | |
|---|---|
| Elements | 5 — `KPCL-RTPS-U2` plus `-BLR`, `-TUR`, `-GEN`, `-TUR-BRG1` |
| Attributes | 16, every one resolved to a `U2_*` tag |
| Tag rows | created from the attribute templates, carrying units, span, scan rate and deadbands |
| Audit rows | one per element and per tag, with actor and reason (§433) |

```
fleet query — 'this attribute across every element of this template':

  Bearing.Vibration
    KPCL-RTPS-U1-TUR-BRG1    U1_BEARING_VIB
    KPCL-RTPS-U2-TUR-BRG1    U2_BEARING_VIB
  Boiler.MainSteamTemperature
    KPCL-RTPS-U1-BLR         U1_MS_TEMP
    KPCL-RTPS-U2-BLR         U2_MS_TEMP

latest value across the fleet, with quality:
  KPCL-RTPS-U1-BLR       U1_MS_TEMP     536.89 Good
  KPCL-RTPS-U2-BLR       U2_MS_TEMP   no data
```

Unit 2 reporting "no data" is correct and worth stating: the asset exists and
its tags exist, but nothing is producing values for it yet. The simulator models
one unit; Stage 10 brings the hardware rig in as a second. The asset model does
not pretend otherwise, and the fleet query says so rather than showing a zero.

## Why creating the tags matters

If instantiating a unit had left its tag rows to be written by hand, adding a
unit would still be a manual remapping job wearing a template's clothes. The
attribute template therefore carries engineering unit, instrument span, scan
rate and both deadbands, and `instantiate()` creates any tag that does not yet
exist. `U2_MS_TEMP` came out with `degC`, span 0–600, 500 ms scan, ExcDev 1.2
and CompDev 2.4 — the same configuration Unit 1 has, without anyone restating it.

## Tests

`pytest engine/ -q` → **11 passed**.

| Test | Asserts |
|---|---|
| `test_adding_a_second_unit_is_one_call_and_remaps_nothing` | the criterion: one call, whole subtree, every attribute on a `U9_*` tag |
| `test_the_new_units_tags_were_created_with_their_configuration` | span, scan rate and deadbands carried from the template |
| `test_the_fleet_query_now_returns_both_units` | the query an asset framework exists for |
| `test_the_fleet_query_spans_derived_templates` | asking a base template finds elements of derived ones |
| `test_adding_a_unit_is_recorded_in_the_audit_log` | actor and reason on every element and tag (§433) |
| `test_a_derived_template_overrides_its_base` | `Fuel=coal`, `RatedCapacity=210` beat the base's values |
| `test_a_derived_template_inherits_what_it_does_not_restate` | `GrossGeneration` declared once, on the base |
| `test_an_asset_code_is_never_reused` | a repeated code is refused (§392) |
| `test_the_hierarchy_is_navigable_from_the_top` | levels correct from Enterprise to Equipment |
| `test_a_pattern_the_context_cannot_satisfy_is_an_error` | no silent `_MS_TEMP` |

## Defects found and fixed

| Defect | Consequence had it shipped |
|---|---|
| `DISTINCT ON` over the derivation chain had no ordering by depth, so which template's version of an attribute won was arbitrary. `ThermalUnit210.Fuel = "coal"` lost to `GeneratingUnit`'s `"unknown"` | Derived templates would silently not override their base — the feature would appear to work while doing nothing |
| `attribute_across_template` matched the template name exactly, so asking a **base** template returned nothing at all | `GeneratingUnit.GrossGeneration` found zero units although two exist. In a mixed fleet — 210 MW beside 500 MW — the fleet-wide query is exactly the one that would silently return empty |
| Dead code left in `compose()` from an interrupted edit (`... if False else None`) | Harmless but false: removed rather than explained |
| Asset tests assumed the config contained no Unit 2, then the config gained one | Five tests failed the moment the model they test was used for real. Tests now use a code the config does not |

## Naming convention (§385)

Documented in `engine/README.md`: ISA-95 equipment hierarchy for elements with
permanent hyphenated asset codes, `<unit>_<measurement>` for tags, PascalCase
measurement names for attributes, and the distinction between derivation
(is-a) and composition (has-a).

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
