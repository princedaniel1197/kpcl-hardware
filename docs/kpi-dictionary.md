# KPI dictionary

Generated from `kpi_definition` by `engine.kpi.dictionary_markdown`.
Do not edit by hand: the database is the source of truth, and a
hand-edited copy would describe equations that are not the ones being
computed.

Generated 2026-09-22T17:52:33+00:00.

## AuxiliaryPowerConsumption  (v1)

Auxiliary power consumption as a percentage of gross generation.

| | |
|---|---|
| Classification | `calculated` |
| Equation | `aux / mw * 100` |
| Engineering unit | % |
| Inputs | `mw` = GrossGeneration, `aux` = AuxiliaryPower |
| Constants | — |
| Reference value | 8.5 |
| Validity range | [0.0, 25.0] |
| Bad-data treatment | `propagate` |
| Calculation frequency | 60000 ms |
| Valid from | 2026-09-22T17:50:18+00:00 |
| Valid to | 2026-09-22T17:52:32+00:00 |

## AuxiliaryPowerConsumption  (v2)

Auxiliary power consumption as a percentage of gross generation.

| | |
|---|---|
| Classification | `calculated` |
| Equation | `aux / mw * 100` |
| Engineering unit | % |
| Inputs | `mw` = GrossGeneration, `aux` = AuxiliaryPower |
| Constants | — |
| Reference value | 8.5 |
| Validity range | [0.0, 30.0] |
| Bad-data treatment | `propagate` |
| Calculation frequency | 60000 ms |
| Valid from | 2026-09-22T17:52:32+00:00 |
| Valid to | 2026-09-22T17:52:33+00:00 |

## AuxiliaryPowerConsumption  (v3)

Auxiliary power consumption as a percentage of gross generation.

| | |
|---|---|
| Classification | `calculated` |
| Equation | `aux / mw * 100` |
| Engineering unit | % |
| Inputs | `mw` = GrossGeneration, `aux` = AuxiliaryPower |
| Constants | — |
| Reference value | 8.5 |
| Validity range | [0.0, 25.0] |
| Bad-data treatment | `propagate` |
| Calculation frequency | 60000 ms |
| Valid from | 2026-09-22T17:52:33+00:00 |
| Valid to | current |

## GrossUnitHeatRate  (v1)

Gross unit heat rate: heat input per unit of electrical output. Coal flow times gross calorific value, divided by gross generation.

| | |
|---|---|
| Classification | `calculated` |
| Equation | `coal_flow * gcv / mw` |
| Engineering unit | kcal/kWh |
| Inputs | `mw` = GrossGeneration, `coal_flow` = CoalFlow |
| Constants | `gcv` = 3500.0 |
| Reference value | 2450.0 |
| Validity range | [1800.0, 5000.0] |
| Bad-data treatment | `propagate` |
| Calculation frequency | 60000 ms |
| Valid from | 2026-09-22T17:50:18+00:00 |
| Valid to | 2026-09-22T17:52:01+00:00 |

## GrossUnitHeatRate  (v2)

Gross unit heat rate: heat input per unit of electrical output. Coal flow times gross calorific value, divided by gross generation.

| | |
|---|---|
| Classification | `calculated` |
| Equation | `coal_flow * gcv / mw` |
| Engineering unit | kcal/kWh |
| Inputs | `mw` = GrossGeneration, `coal_flow` = CoalFlow |
| Constants | `gcv` = 3800.0 |
| Reference value | 2450.0 |
| Validity range | [1800.0, 5000.0] |
| Bad-data treatment | `propagate` |
| Calculation frequency | 60000 ms |
| Valid from | 2026-09-22T17:52:01+00:00 |
| Valid to | 2026-09-22T17:52:12+00:00 |

## GrossUnitHeatRate  (v3)

Gross unit heat rate: heat input per unit of electrical output. Coal flow times gross calorific value, divided by gross generation.

| | |
|---|---|
| Classification | `calculated` |
| Equation | `coal_flow * gcv / mw` |
| Engineering unit | kcal/kWh |
| Inputs | `mw` = GrossGeneration, `coal_flow` = CoalFlow |
| Constants | `gcv` = 3500.0 |
| Reference value | 2450.0 |
| Validity range | [1800.0, 5000.0] |
| Bad-data treatment | `propagate` |
| Calculation frequency | 60000 ms |
| Valid from | 2026-09-22T17:52:12+00:00 |
| Valid to | current |

## SpecificCoalConsumption  (v1)

Coal consumed per unit of electrical output.

| | |
|---|---|
| Classification | `calculated` |
| Equation | `coal_flow / mw` |
| Engineering unit | kg/kWh |
| Inputs | `mw` = GrossGeneration, `coal_flow` = CoalFlow |
| Constants | — |
| Reference value | 0.7 |
| Validity range | [0.2, 1.5] |
| Bad-data treatment | `propagate` |
| Calculation frequency | 60000 ms |
| Valid from | 2026-09-22T17:50:18+00:00 |
| Valid to | current |

## Bad-data treatment

Every definition declares `propagate`, and the schema permits no other
value. A KPI with a Bad input returns Bad with a reason naming the
offending input. It does not return zero, the last good value, or an
interpolation (§318).

A result that is not Good carries no number at all. `KpiResult` raises
if constructed with both, so there is no path by which a bad result
can be read as a figure.

## Not implemented, deliberately

Cylinder efficiency and condenser performance require published steam
tables. Under §486 they are implemented from those tables or not at
all, and they are not implemented here. An approximation carrying no
provenance would be worse than their absence.
