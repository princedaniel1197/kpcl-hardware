# collector — acquisition (Stage 3)

Subscribes to the Stage 1 OPC UA server, forwards to the archive, buffers
locally when the archive is unreachable, and replays in ascending
source-timestamp order on restore.

```bash
make seed-tags     # tag configuration into the archive, from config/
make collector     # run it; SIGTERM or Ctrl-C stops it cleanly
make outage-test   # the Stage 3 acceptance test (~9 minutes, stops the database)
make deadband-evidence   # source deadband on vs off, measured
```

## The session is read-only

There is no write method in this package. Not commented out, not behind a flag,
not unused (§303, §315). `test_readonly.py` asserts it by parsing the package's
own source, recursively: no call to `write_value`, `write_attribute`,
`set_value`, `call_method` or their siblings, and no node-management call
(`add_variable`, `add_object`, `delete_nodes`, ...) — creating or deleting nodes
in a server's address space is a write too — in code **or in a comment**, and no
function defined with those names. It also plants a violation in a throwaway
file and checks the rule catches it, because a test that cannot fail proves
nothing.

`ArchiveSink.write` exists and is allowed: it writes to the *archive*. Nothing
writes to the *source*.

## What the rules cost, concretely

**Measurement time is not arrival time (§335).** `Sample` carries `source_ts`
and `server_ts` as separate fields, populated from the DataValue's own
timestamps. A DataValue with no `SourceTimestamp` is **refused and counted**
(`DROP_NO_SOURCE_TS`), not stamped with the receipt time — inventing one would
be indistinguishable from the failure this project exists to prevent. One with
no `ServerTimestamp` is kept with `server_ts` **NULL** and counted
(`NO_SERVER_TS`). It used to be filled with the source timestamp, which is the
exact fingerprint T-07 exists to detect.

**Quality is never assumed.** A DataValue with no StatusCode is refused and
counted (`DROP_NO_STATUS`); it used to be stored as Good. (On the wire an omitted
StatusCode means Good and asyncua decodes it so; one that reaches the handler as
nothing at all has no quality.) A value that is not a number — a string, an
array — is refused and counted (`DROP_UNSTORABLE`), not stored empty with
whatever quality came with it.

**A Bad sample has no value.** Measured in Stage 1: an OPC UA DataValue with a
Bad StatusCode carries `Value = None` (Part 4). `Sample.value` is
`float | None` all the way through, and `None` reaches the archive as NULL.
There is nothing to substitute and substituting would be forbidden anyway
(§318).

**Quality is the numeric StatusCode**, from `DataValue.StatusCode.value`. A
change in quality is emitted as its own event even when the value did not move.

## Deadband at the source: per server

Whether a tag's ExcDev is applied at the source, as an OPC UA DataChangeFilter
with `Trigger = StatusValue`, is decided per server in `config/sources.json`,
matched on the server's own ApplicationUri. It is **on by default**: at 34,700
I/O the deadband at the source is what keeps unchanged values off the wire
(§317).

It is **off for the simulator**, because asyncua 2.0.1's server ANDs the
data-change trigger with the deadband test, so a status change on a value inside
the deadband is never sent. Measured on 23 September
(`make deadband-evidence`): with the source deadband on, 6.8 notifications/s
and **zero** Bad notifications when a tag was forced to BadDeviceFailure; off,
22.0/s and the Bad notification arrived. `test_session.py` pins the defect: if
asyncua fixes it, that test fails and the setting can be revisited.

## Buffering, and the subtlety in the drain

On a forward failure the batch goes to a SQLite buffer on disk and the
subscription keeps running (§319). On restore the buffer is replayed in
ascending `source_ts` order, carrying original timestamps and quality, through
`ON CONFLICT (tag_id, source_ts) DO NOTHING` (§382, §648). The buffer is
written with `synchronous=FULL` — a buffered row survives a power cut — and its
timestamps are integer microseconds, which sort chronologically by definition.

**Live samples during a drain.** The drain runs inside the forwarder, so while it
runs nothing takes samples off the in-memory queue. Between every drained batch
the drain moves whatever has queued up into the buffer, where the ordered read
puts it after the older rows. Live samples never reach the archive ahead of
older buffered ones, and are never held only in memory for longer than one
batch. (An earlier version of this README said live samples were buffered
during a drain. They were not; they waited in memory for the whole drain.)

Rows are forgotten from the buffer only *after* the archive has committed them,
so a crash mid-drain replays rather than loses.

The buffer is bounded and configurable. When full, the **oldest** rows are
discarded — a ring, as PI's buffering behaves. Refusing new samples instead
would mean the most recent state of the plant is the part you lose, which is
worse for an operator watching a fault develop. Either way it is a real loss:
it is counted (`BUFFER_LOST`), and every discarded range — tag, run, sequence
numbers, source-time span — is written to the archive's `collector_loss` in the
same transaction as the discard, so it can be found by query.

On SIGTERM or SIGINT, anything still in memory — the queue, and any
compressor's open segment — is flushed to the buffer before exit, and the next
start drains it. (Until 23 September only the outage-test harness did this;
`python -m collector` itself did not.) A SIGKILL cannot be caught; it costs at
most the batch linger, and for a compressed tag up to max_time.

## Loss detection

Two mechanisms, for the two places a sample can be lost.

**Between the source and the collector**: the OPC UA server numbers every
NotificationMessage on a subscription, and a keep-alive carries the number the
next one will use (Part 4). A hole is a message the collector never received,
counted as `PUBLISH_MISSED`. `test_session.py` drops a real message against a
real server and checks the count.

**Between the collector and the archive**: each collector start is a run
(`collector_run`), and every sample the run decides to archive is numbered per
tag. The run and the number go into the archive with the row. The view
`sample_seq_gap` lists every hole in a run's numbers, with how many rows another
run holds in that stretch (redundant collectors: first write wins) and how many
`collector_loss` accounts for.

What it cannot see, stated rather than implied: a missing sample before a run's
first archived row or after its last has no neighbours to make a hole. Such
absences occur harmlessly at a run's start, when a value unchanged since an
earlier run archived it is delivered again on subscribe and the earlier row is
kept. The Stage 3 test accounts for every one of them explicitly.

The first version numbered samples as the collector received them and checked
that its own numbers were consecutive — which they always are. It could not
detect anything, and never wrote the numbers anywhere downstream.

## Health (§462)

The collector publishes its own health as tags in the same archive, through the
same pipeline: link state, buffer depth and percentage, samples per second,
seconds since last successful forward, CPU, peak resident memory — and the
counts that make a loss or a refusal visible: NotificationMessages missed,
samples lost to buffer overflow, DataValues refused (no SourceTimestamp, no
StatusCode, not a number), samples stored without a ServerTimestamp, repeats
absorbed, and configured tags not found in the source. Each used to be a log
line at most. The health tags are generated from `health.py` by
`collector.seed`, so the code and the tags cannot drift apart. CPU and memory
come from `resource.getrusage`, not psutil — no dependency beyond the set the
build plan names.

Health samples carry **no server timestamp** (NULL): no OPC UA server put them
on a wire.

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

Where a tag lives in the source address space is `tag.source_path`, with **no
default**. A tag without one, or not found at it, is refused with an error and
counted (`TAGS_UNRESOLVED`); every other tag is still acquired. The column used
to default to `Unit1`, which quietly filed Unit 2's tags there.

## The event stream

`EventStream` emits a small JSON-serialisable event for every meaningful
occurrence — value received, value refused, buffered, forwarded, drain started,
drained, overflow, high water, publish gap detected, quality changed, link
state. The collector serves it itself (`ws://127.0.0.1:8090/events`), not
through the archive, and `/health` beside it. Stage 12
consumes it; nothing here knows what a browser is. A subscriber that stops
reading loses its oldest events rather than stalling acquisition.

## asyncua notes, measured

| Behaviour | Consequence |
|---|---|
| `subscribe_data_change()` has **no** `monitoring_filter` argument in 2.x | A source deadband goes through `subscription.deadband_monitor(node, value)` |
| `deadband_monitor` sets `Trigger = StatusValue`, **but asyncua's server then ANDs the trigger with the deadband** | A status change inside the deadband is never sent. An earlier version of this table said the StatusValue trigger made it visible; measured, it does not. Hence `config/sources.json` |
| A subscription reports **(value, status)** changes; a Bad write's value is dropped, so two Bad writes with different numbers are one notification | What the simulator's ledger (`sim/ledger.py`) counts as "published" |
| `read_data_value()` raises on non-Good status by default | The max-time read passes `raise_on_bad_status=False` |
| The server numbers NotificationMessages; keep-alives carry the next number | How `PUBLISH_MISSED` is detected |

## Tests

```bash
pytest collector/ -q      # buffer, pipeline, session, compression, read-only
make outage-test          # the acceptance test: really stops the database
```

## Compression (Stage 4)

`compression.py` implements two-stage compression as PI does it: exception
reporting against ExcDev, then swinging-door against CompDev, with
ExcDev ≈ CompDev/2 (§442).

```bash
make compression-report    # through the live pipeline, against the simulator
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
CompDev; `test_compression.py` now carries an independent textbook
implementation and shows it at 1.59–1.68 × CompDev on its test sine. So the cone decides *when* to archive and a verification step decides
*which* point, walking back until interpolating to it keeps every discarded
point within CompDev. Three separate paths needed that verification before the
bound held end to end: the corridor itself, points discarded by exception
reporting (which the door now witnesses even though it never receives them),
and forced archives.

The result is a guarantee of **CompDev end to end against the raw series**,
stronger than the ExcDev+CompDev two-stage compression conventionally claims.

**Ratios are a property of the deadband relative to the signal's noise**, not of
the algorithm. Measured through the live pipeline on 23 September (120 s,
steady load): `U1_MS_TEMP` 34.4:1, `U1_BEARING_VIB` 1.2:1, 15 of 15 tags within
CompDev; over a start-up on 22 September `U1_MS_TEMP` was 19.0:1. The vibration tag's CompDev is deliberately below its
noise floor because excursions are the reason it exists, so detail is kept and
the poor ratio accepted. Rationale is recorded per tag in
`config/unit1_tags.json`.

**In the live pipeline, per tag, off by default.** A tag with `compress = true`
(and ExcDev, CompDev and max_time set — the schema insists) is compressed by
`python -m collector`. Every tag is `false` as shipped, because compression
discards samples, which is the opposite of the guarantee Stage 3 tests; with it
on, "zero loss" would mean "nothing that cannot be reconstructed within
CompDev", a different claim. Costs, stated: an archived point reaches the
archive up to max_time after it was measured; a SIGKILL loses the open segment;
two redundant collectors compressing independently archive the union of their
choices, and the CompDev bound is proved for each choice, not for the union.
max_time is required with CompDev: it is what bounds the segment the door holds,
and checking it costs O(n²) per segment in n = max_time × rate.

## OMF output (Stage 9)

The collector's output format is configuration. Setting `CRPMS_OMF_URL` makes
OMF its only output:

```bash
export CRPMS_OMF_URL=https://pi.example.com/piwebapi/omf
export CRPMS_OMF_USER=... CRPMS_OMF_PASSWORD=... CRPMS_OMF_TOKEN=...
make collector
```

Pointing at a real PI Web API OMF endpoint is those variables and **no code
change**. Unset them and the collector writes directly to TimescaleDB, which is
the default and is what Stage 3's zero-loss test measured.

`archive/omf_receiver.py` accepts the same messages and writes to TimescaleDB,
so the whole chain can be exercised without a PI licence.

**SourceTime is the OMF index**, so a historian orders and de-duplicates on when
the value was produced; ServerTime rides alongside, explicitly `null` when no
server stamped the value. **Quality uses OMF's
`isquality`**, so the numeric StatusCode is a designated quality property rather
than an ordinary number.

**One hazard worth knowing.** The OMF specification defaults an omitted numeric
property to `0`. Expressing "no value" by omitting `Value` would therefore store
a zero — the substitution §318 forbids, arriving through the wire format. The
emitter always sends `Value` explicitly, `null` when there is none, and the
receiver refuses a message that omits it — and likewise `ServerTime`, and
`Quality`, whose omitted default of 0 would be Good.

On the OMF path the per-run sequence numbers are not carried: the OMF type has
no property for them and PI no field. Registering the run and recording
buffer-overflow losses still use SQL against the configuration database, as
reading tag configuration always has.

This receiver is not PI. Whether a real AVEVA endpoint accepts these exact
messages has not been tested, because this project has no PI licence.
