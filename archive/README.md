# archive — schema and migrations (Stage 2)

Numbered SQL migrations in `migrations/`, applied by `archive/migrate.py`.

```bash
make migrate           # apply pending migrations
make migrate-status    # list applied and pending
make loadtest          # Stage 2 acceptance test: 10M rows, ~1 GB
```

Migrations are **immutable once applied**. The runner records a checksum of
each file and refuses to run if an applied file has changed, rather than
pretending the database matches the repository. A correction is a new numbered
migration — `007` exists because of exactly that.

A file marked `-- migrate:no-transaction` is applied with autocommit, because
TimescaleDB refuses to create a continuous aggregate or add a policy inside a
transaction block.

## The two tables everything turns on

**`sample`** — `(tag_id, source_ts, server_ts, value, quality, collector_run, seq)`.

`source_ts` is when the value was produced; `server_ts` is when it went on the
wire. Neither is ever derived from the other (§335). `source_ts` is `NOT NULL`;
`server_ts` is **nullable** since migration 014, because when no server stamped a
value the honest record is NULL — the column used to be `NOT NULL`, and that is
why the collector filled a missing ServerTimestamp with the source timestamp,
producing exactly the identical-timestamps fingerprint T-07 looks for. The
collector's own health samples have no server_ts. `sample_decoded` exposes
`server_ts - source_ts` as `transit`, NULL where there is no server_ts.

`collector_run` and `seq` (migration 014) say which collector run archived the
row and its place in that run's per-tag numbering. `sample_seq_gap` lists the
holes, and says whether another run (first write wins) or `collector_loss` (the
buffer-overflow ledger) accounts for each. Rows written other than by a
numbered collector run — the load test, the OMF receiver, anything from before
23 September — have neither.

The primary key is `(tag_id, source_ts)`. Replay cannot duplicate because the
schema forbids it, not because the writer remembers not to (§382, §648). The
same key serves the "all samples for one tag over 24 hours" query — measured at
38 ms median over 10 million rows, against a 5 s criterion.

`value` is **nullable** and `quality` is **bigint**. Both differ from the build
plan's wording, and both follow from the plan's own rules:

- A Bad DataValue carries `Value = None` per OPC UA Part 4 (measured in Stage 1).
  A Bad sample has no number; storing 0 would be the substitution §318 forbids.
- `BadDeviceFailure` is 2,156,593,152, which exceeds smallint (32,767) and int4
  (2,147,483,647) alike. A smallint column cannot hold an OPC UA StatusCode, so
  it cannot satisfy the rule that quality *is* the StatusCode.

**`sample_1min`** — the continuous aggregate. It averages **Good samples only**
and carries `sample_count`, `good_count`, `uncertain_count` and `bad_count`
alongside. A mean over a window that was half Bad is not the same quantity as a
mean over a clean one, and a chart that cannot tell the difference is the
smoothing this project exists to avoid.

## Quality decoding

`quality` is stored numerically. `quality_class(bigint)` decodes the OPC UA
severity from the top two bits — 00 Good, 01 Uncertain, 10 Bad — and
`sample_decoded` and `kpi_value_decoded` expose it. The number remains the
stored truth.

## Policies

| Policy | Setting |
|---|---|
| Retention on `sample` | 10 years |
| Continuous aggregate refresh | every minute, 3 h lookback |
| Native compression | chunks older than 7 days |

TimescaleDB's native compression is not the two-stage process compression of
Stage 4. Stage 4 decides which samples are worth archiving at all; this stores
the ones that survived more cheaply.

## Tables

| Table | Purpose | Clause |
|---|---|---|
| `element_template`, `attribute_template` | templates, including derived ones | §341, §392 |
| `element`, `attribute` | the hierarchy, self-referencing, with permanent asset codes | §392 |
| `tag` | name, units, instrument span, scan rate, compression deadbands and whether to compress (`compress`, default false), where it lives in the source (`source_path`, no default) | §436, §442 |
| `sample` | the hypertable | §335, §382 |
| `event_frame`, `event_milestone`, `event_frame_summary` | Stage 8 | §472, §475 |
| `kpi_definition`, `kpi_value` | versioned equations and traceable results | §320, §379, §381 |
| `audit_log` | actor, timestamp, old value, new value, reason — **append-only**: UPDATE, DELETE and TRUNCATE are refused by trigger (migration 015) | §433 |
| `collector_run`, `collector_loss` | each collector start; each range the buffer discarded on overflow | §319, §462 |
| `sldc_*` | the Karnataka SLDC recorder's own tables (`scraper/migrations/`), not part of the staged build | — |

`kpi_definition.bad_data_treatment` accepts exactly one value, `propagate`. The
column exists because the tender asks for the treatment to be declared, not
because substitution is an option; a CHECK constraint refuses anything else.

## Tests

```bash
pytest archive/ -q                   # schema and audit, each in a rolled-back transaction
make loadtest                        # the 10M-row acceptance test
```
