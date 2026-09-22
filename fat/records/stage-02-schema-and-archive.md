# Stage 2 — Schema and archive

| | |
|---|---|
| Stage | 2 — Schema and archive |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 2 |
| Acceptance criterion | Insert 10 million synthetic rows; a 24-hour single-tag query returns in under 5 seconds (§528). Inserting the same `(tag_id, source_ts)` twice does not duplicate. |
| Date performed | 2026-09-22 |
| Host | macOS Apple silicon; PostgreSQL 16.15, TimescaleDB 2.30.1 |
| **Result** | **PASS** |

## Test 1 — 24-hour single-tag query over 10 million rows

```
  load: 110.3s (90,673 rows/s)
  rows in sample : 9,999,990
  on disk        : 1062 MB
  chunks         : 9
  data spans 2026-01-01 00:00:00+00 .. 2026-01-09 06:24:44+00

  query plan:
    Custom Scan (ChunkAppend) on sample (actual time=0.011..9.113 rows=86400 loops=1)
      Order: sample.source_ts
      Buffers: shared hit=1057
      ->  Index Scan using "8_sample_pkey" on _hyper_3_8_chunk
            Index Cond: ((tag_id = '1') AND (source_ts >= ...) AND (source_ts < ... + '24:00:00'))

  24-hour single-tag query: 86,400 rows
    runs   : 54ms, 41ms, 38ms, 36ms, 38ms
    median : 38 ms
    max    : 54 ms
```

**PASS — 54 ms worst case against a 5,000 ms criterion, a margin of about 90×.**

The plan is an index scan on the `(tag_id, source_ts)` primary key with chunk
exclusion, which is the reason no separate index was added: the key that makes
replay idempotent is also the key this query wants.

The window is derived from the data actually present rather than hard-coded.
An earlier version of this test used a fixed date, fell outside the data, and
**reported PASS on a query that returned zero rows in zero milliseconds**. The
test now asserts the query returned ~86,400 rows before accepting the timing —
a fast query over nothing proves nothing.

## Test 2 — idempotency

```
  re-inserted 1,000 existing rows: 714,285 -> 714,285
    CRITERION (no duplicates): PASS
```

**PASS.** Re-inserting 1,000 rows that were already present changed the count
by zero. `test_same_tag_and_source_ts_cannot_duplicate` additionally asserts
that without `ON CONFLICT` the second insert raises `UniqueViolation` — the
schema refuses the duplicate rather than a code path remembering not to write it.

## Test 3 — quality and value across 10 million rows

```
   class   |  quality   |  count  | with_value | null_value
 Good      |          0 | 9503018 |    9503018 |          0
 Uncertain | 1083375616 |  296968 |     296968 |          0
 Bad       | 2156593152 |  200004 |          0 |     200004
```

**PASS.** Every Bad row carries a NULL value and every Uncertain row keeps its
number, matching what the protocol actually delivers (measured in Stage 1).
Bad and Uncertain remain distinguishable at rest, not only in flight.

## Test 4 — continuous aggregate

```
         bucket         | avg_good | sample_count | good_count | uncertain_count | bad_count
 2026-01-05 00:00:00+00 |   130.00 |           60 |         56 |               2 |         2
 2026-01-05 00:01:00+00 |   131.58 |           60 |         57 |               2 |         1
```

**PASS.** The one-minute aggregate averages **Good samples only** and carries
the counts that qualify the average. A mean over a window that was half Bad is
not the same quantity as a mean over a clean one, and a consumer can always ask
how much of the minute was actually good.

## Automated schema tests

`pytest archive/test_schema.py -q` → **16 passed**. Each runs in a rolled-back
transaction and leaves nothing behind.

Covered: StatusCode storage, duplicate refusal, Bad-has-no-value, severity
decoding, transit exposure, NOT NULL on both timestamps and on quality,
attribute tag-xor-static, asset code uniqueness, one open event frame per
element and template, one current KPI version per name, KPI supersession,
refusal of any `bad_data_treatment` other than `propagate`, a Bad KPI value
carrying a reason and no number, audit before/after, and tag range ordering.

## Deviations from the build plan, and why

| Plan says | Built | Why |
|---|---|---|
| `sample.quality smallint` | **`bigint`** | `BadDeviceFailure` is 2,156,593,152. That exceeds smallint (32,767) *and* int4 (2,147,483,647). The plan's own rule — quality is the numeric OPC UA StatusCode — cannot be satisfied by a smallint column. The rule decides. |
| `sample.value double precision` | **nullable** `double precision` | Measured in Stage 1: a Bad DataValue carries `Value = None` per OPC UA Part 4. A Bad sample has no number, and storing 0 instead would be the substitution §318 forbids. |
| separate index for the 24-hour query | **none added** | The `(tag_id, source_ts)` primary key already serves it; the plan above confirms an index scan. An extra index would be write cost for no read benefit. |

## Defects found and fixed during this stage

| Defect | Fix |
|---|---|
| The load test reported PASS while its query returned 0 rows, because the window fell outside the data | Window derived from the data; row count asserted before the timing is accepted |
| `kpi_definition` CHECK required `valid_to > valid_from`, making it impossible to supersede a definition in the transaction that created it (`now()` is transaction-start time) | Migration `007` relaxes it to `>=`. A zero-length window records a definition that never produced a value |

The second was found by a test, and fixing it exercised the migration runner's
immutability guard: an applied migration cannot be edited — the runner refuses
with a checksum mismatch — so the correction is a new numbered migration.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
