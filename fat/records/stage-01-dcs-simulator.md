# Stage 1 — The DCS simulator

| | |
|---|---|
| Stage | 1 — The DCS simulator |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 1 |
| Acceptance criterion | An asyncua client reads U1_MW ten times and prints value, StatusCode name, SourceTimestamp and ServerTimestamp. The two timestamps differ on every sample. Forcing Bad through the control API changes the StatusCode within one scan. |
| Date performed | 2026-09-22 |
| Host | macOS (Darwin 25.6.0), Apple silicon; Python 3.11.0; asyncua 2.0.1 |
| **Result** | **PASS** |

## Test 1 — ten reads of U1_MW

Run against a live OPC UA server over a real socket, sampling while the unit was
taking load so that the tag was genuinely changing. A tag pinned at zero would
not test what the criterion claims to.

```
     value  StatusCode   SourceTimestamp                  ServerTimestamp                  delta_ms
     0.101  Good         2026-09-22 16:16:53.024312+00:00 2026-09-22 16:16:53.042202+00:00    17.89
     5.112  Good         2026-09-22 16:16:53.142755+00:00 2026-09-22 16:16:53.163302+00:00    20.55
    11.908  Good         2026-09-22 16:16:53.264434+00:00 2026-09-22 16:16:53.277459+00:00    13.03
    19.343  Good         2026-09-22 16:16:53.495776+00:00 2026-09-22 16:16:53.513221+00:00    17.45
    29.588  Good         2026-09-22 16:16:53.615583+00:00 2026-09-22 16:16:53.629290+00:00    13.71
    42.473  Good         2026-09-22 16:16:53.730607+00:00 2026-09-22 16:16:53.749358+00:00    18.75
    76.463  Good         2026-09-22 16:16:53.967907+00:00 2026-09-22 16:16:53.982270+00:00    14.36
    95.637  Good         2026-09-22 16:16:54.082662+00:00 2026-09-22 16:16:54.101508+00:00    18.85
   115.543  Good         2026-09-22 16:16:54.203422+00:00 2026-09-22 16:16:54.222281+00:00    18.86
   136.347  Good         2026-09-22 16:16:54.324306+00:00 2026-09-22 16:16:54.342975+00:00    18.67
```

**PASS.** Ten samples, value rising 0.101 → 136.347 MW, SourceTimestamp and
ServerTimestamp different on every one, by 13.03 to 20.55 ms. ServerTimestamp is
later than SourceTimestamp on every sample; the reverse would mean the source
timestamp had been invented at write time.

Asserted additionally: at least two distinct values across the ten reads, so the
test cannot pass against a static tag.

## Test 2 — forcing quality through the control API

Live, against the running simulator, driving the HTTP control API on port 8081
while reading the tag over OPC UA. Scan interval 500 ms.

```
phase: STEADY

BEFORE — tag healthy
  U1_COAL_FLOW      142.31 t/h  Good                        src=16:18:38.671 srv=16:18:38.713 (+41.7ms)

POST /quality/U1_COAL_FLOW {"quality":"BadDeviceFailure"}
  -> {'tag': 'U1_COAL_FLOW', 'quality': 'BadDeviceFailure', 'status_code': 2156593152}

AFTER one scan
  U1_COAL_FLOW         (null)   BadDeviceFailure            src=16:18:39.171 srv=16:18:39.217 (+46.2ms)

POST /quality/U1_COAL_FLOW {"quality":"UncertainSensorNotAccurate"}
  U1_COAL_FLOW      143.21 t/h  UncertainSensorNotAccurate  src=16:18:39.670 srv=16:18:39.705 (+35.0ms)

DELETE /quality/U1_COAL_FLOW
  U1_COAL_FLOW      141.56 t/h  Good                        src=16:18:40.171 srv=16:18:40.207 (+36.0ms)
```

**PASS.** The StatusCode changed within one 500 ms scan of the API call, and
recovered within one scan of the restore. The source timestamp remained a real
measurement time throughout, including while the tag was Bad.

## Automated tests

`.venv/bin/python -m pytest sim/ -q` → **13 passed**.

| Test | Asserts |
|---|---|
| `test_ten_reads_of_u1_mw_have_distinct_timestamps` | the stage criterion above |
| `test_source_timestamp_is_not_receipt_time` | two reads of an unchanged value return the same SourceTimestamp — if it moved it would be receipt time |
| `test_forcing_bad_takes_effect_within_one_scan` | Bad within one scan, recovery within one scan |
| `test_forcing_uncertain` | Uncertain is neither Good nor Bad |
| `test_forcing_one_tag_does_not_affect_others` | forcing is per-tag |
| `test_every_tag_exists_with_units_and_eurange` | engineering units and EURange on every analogue (§436) |
| `test_digitals_are_written_only_on_change` | a digital that did not change was not rewritten (§440) |
| `test_analogues_are_written_every_scan` | analogues are sampled |
| `test_tags_are_not_writable_by_clients` | a client write is refused (§303, §315) |
| `test_control_api_offers_no_way_to_set_a_value` | no route path contains "value" |
| `test_unknown_tag_and_unknown_quality_are_refused` | 404 and 400 |
| `test_startup_reaches_full_load_and_phases_are_ordered` | phases contiguous and in order; breaker closed, 195–225 MW, 2950–3050 rpm |
| `test_reading_a_bad_value_requires_raise_on_bad_status_false` | the asyncua trap below |

Tests drive a real OPC UA server over a real socket and call the control API
over real HTTP. Nothing is mocked.

## Findings carried to later stages

Verified against asyncua 2.0.1, not assumed:

| Finding | Consequence |
|---|---|
| `SourceTimestamp` we set is preserved exactly | Rule 1 holds; no need to pin asyncua 1.x |
| `ServerTimestamp` we set is **discarded** — a year-2000 sentinel returned as "now" | The server owns it. We set only SourceTimestamp; fabricating a ServerTimestamp would be a lie the server corrects anyway |
| `DataValue.StatusCode_` is `StatusCode` in 2.x | Build plan wording is 1.x |
| `read_data_value()` **raises** `UaStatusCodeError` on any non-Good status, by default | **Stage 3**: the collector must pass `raise_on_bad_status=False` or it will crash on exactly the values this project exists to preserve |
| A **Bad** value's `Value` is `None` (VariantType Null), per OPC UA Part 4 | **Stage 2**: `sample.value` must be nullable. **Stage 3**: a Bad sample has no number to store |
| An **Uncertain** value retains its number | Bad and Uncertain are materially different, as §318 requires |

## Defects found and fixed during this stage

| Defect | Fix |
|---|---|
| Noise was added to quantities that are genuinely zero: a unit at standstill read 0.2 MW and 2 rpm | Noise is suppressed when the noiseless value is exactly zero. A false non-zero on a stopped machine would feed Stage 6 checks and Stage 8 event detection |

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
