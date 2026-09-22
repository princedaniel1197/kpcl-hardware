# collector — acquisition (Stage 3)

Subscribes to the Stage 1 OPC UA server, forwards to the archive, buffers
locally when the archive is unreachable, and replays in ascending
source-timestamp order on restore.

```bash
make seed-tags     # tag configuration into the archive, from config/
make collector     # run it
make outage-test   # the Stage 3 acceptance test (~9 minutes, stops the database)
```

## The session is read-only

There is no write method in this package. Not commented out, not behind a flag,
not unused (§303, §315). `test_readonly.py` asserts it by parsing the package's
own source: no call to `write_value`, `write_attribute`, `set_value`,
`call_method` or their siblings, in code **or in a comment**, and no function
defined with those names. It also plants a violation in a throwaway file and
checks the rule catches it, because a test that cannot fail proves nothing.

`ArchiveSink.write` exists and is allowed: it writes to the *archive*. Nothing
writes to the *source*.

## What the rules cost, concretely

**Measurement time is not arrival time (§335).** `Sample` carries `source_ts`
and `server_ts` as separate fields, populated from the DataValue's own
timestamps. If a DataValue arrives with no `SourceTimestamp` at all, the sample
is **dropped with an error**, not stamped with the receipt time — inventing one
would be indistinguishable from the failure this project exists to prevent.

**A Bad sample has no value.** Measured in Stage 1: an OPC UA DataValue with a
Bad StatusCode carries `Value = None` (Part 4). `Sample.value` is
`float | None` all the way through, and `None` reaches the archive as NULL.
There is nothing to substitute and substituting would be forbidden anyway
(§318).

**Quality is the numeric StatusCode**, from `DataValue.StatusCode.value`. A
change in quality is emitted as its own event even when the value did not move,
and the deadband subscription uses `Trigger = StatusValue` so a tag going Bad
while sitting steady still reports.

## Buffering, and the subtlety in the drain

On a forward failure the batch goes to a SQLite buffer on disk and the
subscription keeps running (§319). On restore the buffer is replayed in
ascending `source_ts` order, carrying original timestamps and quality, through
`ON CONFLICT (tag_id, source_ts) DO NOTHING` (§382, §648).

**While the buffer is draining, live samples are buffered too.** If live samples
went straight to the archive while older buffered ones were still replaying, the
archive would receive them out of order — which is exactly what the ordered
drain exists to prevent. Live forwarding resumes only once the buffer is empty.

Rows are forgotten from the buffer only *after* the archive has committed them,
so a crash mid-drain replays rather than loses.

The buffer is bounded and configurable. When full, the **oldest** rows are
discarded — a ring, as PI's buffering behaves. Refusing new samples instead
would mean the most recent state of the plant is the part you lose, which is
worse for an operator watching a fault develop. Either way it is a real loss:
it is logged, counted, and left visible downstream as a sequence gap. A loss you
cannot see is the only kind that actually hurts.

On shutdown, anything still in the in-memory queue is flushed to the buffer
before exit, so stopping the collector does not lose what it had just received.

## Gaps

Each tag carries a monotonic sequence number assigned on receipt. A hole in it
is a fact, not an inference — which is what makes buffer overflow detectable
rather than merely suspected.

## Health (§462)

The collector publishes its own health as tags in the same archive, through the
same pipeline: link state, buffer depth and percentage, samples per second,
seconds since last successful forward, CPU, resident memory, and gap count.
CPU and memory come from `resource.getrusage`, not psutil — no dependency
beyond the set the build plan names.

When the archive is down, health samples are buffered along with everything
else and arrive on recovery, back-dated to when they were measured. The gap in
the health trend during an outage is real, and is itself the evidence of the
outage. That is more honest than a monitoring path that magically survives the
failure it exists to report.

## Configuration is configuration

Scan rates and deadbands are columns on `tag`, read at subscribe time. Adding a
tag or retuning one is a row change, not an edit here (§317, §341).
`config/unit1_tags.json` holds the seed set; `collector.seed` upserts it and
writes every change to `audit_log` with actor, old value, new value and reason
(§433).

One subscription is created per (sampling interval, deadband) group, so each tag
is sampled at its configured rate rather than everything at the fastest rate any
tag needs. Digitals get no deadband: every transition matters (§440).

## The event stream

`EventStream` emits a small JSON-serialisable event for every meaningful
occurrence — value received, buffered, forwarded, drain started, drained,
overflow, high water, gap detected, quality changed, link state. Stage 12
consumes it; nothing here knows what a browser is. A subscriber that stops
reading loses its oldest events rather than stalling acquisition.

## asyncua notes, measured

| Behaviour | Consequence |
|---|---|
| `subscribe_data_change()` has **no** `monitoring_filter` argument in 2.x | Deadbands go through `subscription.deadband_monitor(node, value, deadbandtype=1)` |
| `deadband_monitor` sets `Trigger = StatusValue` | Quality changes report even when the value is inside the deadband — which is what makes a tag going Bad while steady visible |
| `read_data_value()` raises on non-Good status by default | Not used here; the subscription handler receives the DataValue directly and never calls it |

## Tests

```bash
pytest collector/ -q      # buffer and read-only guarantees, fast
make outage-test          # the acceptance test: really stops the database
```

## Compression (Stage 4)

`compression.py` implements two-stage compression as PI does it: exception
reporting against ExcDev, then swinging-door against CompDev, with
ExcDev ≈ CompDev/2 (§442).

```bash
make compression-report    # ratios and reconstruction error against live data
```

**The four exceptions** archive a value regardless of any deadband: quality
changed, the timestamp did not advance, the value is not a number, or max_time
elapsed since the last archived point. Each has a test. The first matters most:
a tag going Bad usually happens while its value is flat, which is exactly when
a deadband would discard it.

**The bound is verified, not assumed.** The textbook swinging door does not
guarantee CompDev on the reconstructed series — the cone test proves a line
within CompDev of every point exists, not that the line actually drawn is that
one. Measured with the cone test alone, realised error reached 1.6–1.9 ×
CompDev. So the cone decides *when* to archive and a verification step decides
*which* point, walking back until interpolating to it keeps every discarded
point within CompDev. Three separate paths needed that verification before the
bound held end to end: the corridor itself, points discarded by exception
reporting (which the door now witnesses even though it never receives them),
and forced archives.

The result is a guarantee of **CompDev end to end against the raw series**,
stronger than the ExcDev+CompDev two-stage compression conventionally claims.

**Ratios are a property of the deadband relative to the signal's noise**, not of
the algorithm. Measured over a live start-up: `U1_MS_TEMP` 19.0:1,
`U1_BEARING_VIB` 1.3:1. The vibration tag's CompDev is deliberately below its
noise floor because excursions are the reason it exists, so detail is kept and
the poor ratio accepted. Rationale is recorded per tag in
`config/unit1_tags.json`.

**Not wired into the live pipeline by default.** Compression discards samples,
which is the opposite of the guarantee Stage 3 tests; Stage 3's zero-loss result
was measured with compression off.
