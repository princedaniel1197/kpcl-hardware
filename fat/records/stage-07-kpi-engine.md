# Stage 7 — KPI engine

| | |
|---|---|
| Stage | 7 — KPI engine |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 7 |
| Acceptance criterion | Force U1_COAL_FLOW to Bad through the Stage 1 control API. Heat rate goes Bad within one calculation cycle, naming coal flow as the cause. It does not go to zero. |
| Date performed | 2026-09-22 |
| Method | `python -m engine.kpi_demo` against the live simulator, collector and archive |
| **Result** | **PASS** — 10 of 10 checks |

## The criterion, on the live system

```
  BEFORE — all inputs Good
    AuxiliaryPowerConsumption   v1        8.40 %         Good
    GrossUnitHeatRate           v1     2366.85 kcal/kWh  Good
    SpecificCoalConsumption     v1        0.68 kg/kWh    Good

  forcing U1_COAL_FLOW to BadDeviceFailure ...

  AFTER — one calculation cycle later
    AuxiliaryPowerConsumption   v1        8.31 %         Good
    GrossUnitHeatRate           v1   (no value) kcal/kWh  Bad
        reason: input CoalFlow (U1_COAL_FLOW) has no value, BadDeviceFailure
    SpecificCoalConsumption     v1   (no value) kg/kWh    Bad
        reason: input CoalFlow (U1_COAL_FLOW) has no value, BadDeviceFailure

  RESTORED — coal flow Good again
    GrossUnitHeatRate           v1     2357.17 kcal/kWh  Good
```

| Criterion | Result |
|---|---|
| Heat rate went Bad within one cycle | **PASS** |
| The reason names coal flow | **PASS** — "input CoalFlow (U1_COAL_FLOW) has no value, BadDeviceFailure" |
| It did **not** go to zero | **PASS** — no value at all |
| Specific coal consumption, which also uses coal flow, went Bad | **PASS** |
| Auxiliary power, which does not, stayed Good | **PASS** |
| Stored Bad, with no value and a reason | **PASS** |
| Stored with the definition version that produced it | **PASS** |
| Recovered when the input was restored | **PASS** |

That auxiliary power stayed Good is as important as the rest: the propagation is
per-KPI according to what each actually consumes, not a blanket alarm.

## It is enforced by the type, not by convention

`KpiResult.__post_init__` raises if a result is constructed with a non-Good
quality and a value. There is no path by which a Bad result can be read as a
figure — `test_a_result_cannot_be_both_bad_and_numeric` asserts it.

Propagation order: any input Bad or absent → Bad naming it; any input Uncertain
→ Uncertain naming it; division by zero → Bad naming the expression; result
outside its validity range → flagged Uncertain with the number withheld.

**Zero load is the case worth stating.** At 0 MW a heat rate is undefined. It is
not infinity and emphatically not zero — a zero heat rate reads as perfect
efficiency, which is the most flattering possible lie a monitoring system can
tell. `test_zero_load_is_bad_not_infinite` covers it.

**A missing recent sample is Bad, not the last good value.** An input with no
sample inside its freshness window is an absence. Reaching back an hour for the
last number that happened to be Good is the substitution §318 forbids.

## Versioning (§320, §379, §381)

Demonstrated live by changing the GCV constant, as a laboratory result would:

```
       name        | version |  gcv   |     valid_from      |      valid_to
 GrossUnitHeatRate |       1 | 3500.0 | 2026-09-22 17:50:18 | 2026-09-22 17:52:01
 GrossUnitHeatRate |       2 | 3800.0 | 2026-09-22 17:52:01 | current

  every stored result traces to the equation that produced it:
    v1: 2 value(s), mean 2362.0 kcal/kWh
    v2: 1 value(s), mean 2563.1 kcal/kWh
```

A definition is never edited once it has produced values. The change closes the
current version and opens the next, and `kpi_value.kpi_version` is stored on the
value row rather than looked up through the definition, so a later edit cannot
silently restate history.

## Equations are data, and data is not executed

Equations live in the database. `safe_eval` walks the parsed expression and
permits arithmetic over named inputs and constants and nothing else. Verified
against attribute access, imports, `open`, lambdas, comprehensions and
`globals()` — each rejected.

## Provenance (§486)

The three KPIs are definitional ratios from standard Indian utility practice:

| KPI | Equation | Unit |
|---|---|---|
| Gross unit heat rate | coal flow × GCV ÷ gross generation | kcal/kWh |
| Auxiliary power consumption | aux power ÷ gross generation × 100 | % |
| Specific coal consumption | coal flow ÷ gross generation | kg/kWh |

None requires a steam table and none is invented here. Measured against the
running simulator they give 2367 kcal/kWh, 8.4% and 0.68 kg/kWh, against
reference values of 2450, 8.5 and 0.70 — plausible for a 210 MW subcritical unit.

**Cylinder efficiency and condenser performance are not implemented.** They
require published steam tables, and under §486 they are implemented from those
tables or not at all. An approximation carrying no provenance would be worse
than their absence.

**The GCV constant is a stated assumption, not a measurement.** This
demonstrator has no coal analysis. On a real unit it comes from the daily
laboratory result, and the heat rate is only ever as good as that figure. It is
recorded as a constant on the definition, so any historical heat rate can be
traced to the GCV that produced it.

## The KPI dictionary (§383)

`docs/kpi-dictionary.md` is generated from the database by
`engine.kpi.dictionary_markdown`, never hand-written, so it cannot drift from
what the engine actually computes. It carries every version, including
superseded ones, with their validity windows.

## Defect found and fixed

The seeder wrote an audit row claiming `field = equation` with **identical old
and new values**, because what had actually changed was the constants. §433
requires the old value, the new value and a reason; naming the wrong field sends
the reader looking in the wrong place, which is worse than no audit row at all.
Fixed to diff the fields and write one row per field that genuinely changed:

```
     field     | old_value | new_value |      reason
 validity_high | 25.0      | 30.0      | superseded by v2
```

## Tests

`pytest engine/test_kpi.py -q` → **21 passed**. Full suite **149 passed**.

## Addendum — 23 September 2026

- **StatusCodes.** A Bad input now gives `BadAggregateInvalidInputs` ("could not
  be derived due to invalid data inputs"); it was `BadDependentValueChanged`,
  which is about writes to a device. An Uncertain input gives
  `UncertainSubNormal`; it was `UncertainSubstituteValue`, "a value that was
  manually overwritten" — the opposite of what the engine does. A result outside
  its validity range gives `UncertainEngineeringUnitsExceeded`; it was a
  sensor-limit code. Results already stored keep the code they were computed
  with, beside the definition version that produced them.
- **Nothing computed KPIs** except this demonstration and the Stage 10 check;
  `kpi_value` held 11 rows and the dashboard showed values twelve hours old.
  `python -m engine` now computes every current definition at its own
  frequency for every element it applies to — derived from the asset model: the
  lowest elements whose subtree holds every input attribute.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
