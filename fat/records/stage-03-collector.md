# Stage 3 — The collector

| | |
|---|---|
| Stage | 3 — The collector |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 3 |
| Acceptance criterion | Start everything. Let it run five minutes. Stop TimescaleDB. Watch the buffer grow. Wait three minutes. Start TimescaleDB. Then verify: no sample missing between the stop and start instants; every restored row carries its original source_ts and quality; zero duplicates; the trend has no gap. |
| Date performed | 2026-09-22 |
| Method | `python -m collector.outage_test --run-seconds 300 --outage-seconds 180`, against the live Stage 1 simulator and the live TimescaleDB container. The container was really stopped with `docker stop`. |
| **Result** | **PASS** |

## The run

```
[1] running normally for 300s ...
    received 4,741 samples, forwarded 4,736, buffer 0

[2] stopping crpms-timescaledb at 2026-09-22T16:48:59.091539+00:00
    archive link down: consuming input failed: server closed the connection unexpectedly
      t+   30s  buffer=   462  received=  5,202  link=DOWN
      t+   60s  buffer=   935  received=  5,686  link=DOWN
      t+   90s  buffer= 1,416  received=  6,175  link=DOWN
      t+  120s  buffer= 1,901  received=  6,636  link=DOWN
      t+  150s  buffer= 2,370  received=  7,122  link=DOWN
      t+  180s  buffer= 2,847  received=  7,603  link=DOWN

[3] starting crpms-timescaledb at 2026-09-22T16:51:59.348267+00:00
    archive link up: reconnected
    draining 2888 buffered samples in source-timestamp order
    drained 2888 samples in 0.2s; buffer now 0
```

Acquisition continued throughout: `received` rose by 2,401 samples while the
archive was unreachable and the buffer grew monotonically to match. The link
came back 0.8 s after the container accepted connections, and 2,888 samples
replayed in 0.18 s.

## Verification

| Criterion | Measured | Result |
|---|---|---|
| No sample missing | 7,623 received, **0 missing from the archive** | **PASS** |
| Zero duplicates | 7,624 rows, 7,624 distinct `(tag_id, source_ts)` | **PASS** |
| Original quality and value preserved | **0 mismatches** across all 7,623 | **PASS** |
| Source time never replaced by receipt time | **0 rows** with `server_ts <= source_ts` | **PASS** |
| Trend has no gap | **2,579 rows** stamped inside the 180 s outage window | **PASS** |
| No loss to buffer overflow | overflow 0, sequence gaps 0 | **PASS** |

The comparison is exact, not statistical. The test subscribes to the collector's
own event stream and records every sample the collector reports receiving, then
compares that ledger against the archive key by key. "No sample missing" is
checked against what was actually acquired, not against an estimate of what
should have been.

The archive holds one row more than the ledger (7,624 vs 7,623): the ledger's
subscriber attaches microseconds after the pipeline starts, so the very first
sample was forwarded before the ledger existed. The archive is a superset of
what was acquired, which is the direction that matters.

## Buffer behaviour

| | |
|---|---|
| Depth at restore | 2,847 |
| Peak depth | 2,888 (live samples continue to buffer while the drain runs) |
| Drain | 2,888 samples in 0.18 s, in ascending `source_ts` |
| Overflow | 0 |

Peak exceeds depth-at-restore because live samples are buffered *during* the
drain as well. If live samples went to the archive while older buffered ones
were still replaying, the archive would receive them out of order — which is
what the ordered drain exists to prevent. Live forwarding resumes only once the
buffer is empty.

## Read-only guarantee (§303, §315)

`pytest collector/test_readonly.py` → 6 passed. The test parses the package's
own source rather than trusting convention: no call to `write_value`,
`write_attribute`, `set_value`, `call_method` or their siblings, in code **or in
a comment**, and no function defined with those names. It also plants a
`write_value()` call in a throwaway file and asserts the rule catches it,
because a test that cannot fail proves nothing.

## Defects found and fixed during this stage

| Defect | How it surfaced | Fix |
|---|---|---|
| Samples sitting in the in-memory queue between `on_sample` and the forwarder were lost when the collector stopped | The first outage run reported 12 "missing" samples, all stamped after the drain had completed | `Pipeline.flush_to_buffer()` moves anything still queued into the durable buffer on shutdown |
| The load-test-style false pass: the harness cancelled the forwarder while samples were still queued, then blamed the collector | Same 12 samples | The harness now stops acquiring first, lets everything received reach the archive, then stops forwarding |
| `subscribe_data_change()` has no `monitoring_filter` argument in asyncua 2.x, so no deadband was ever applied | Collector logged "unexpected keyword argument" and retried forever without acquiring | Deadbands go through `subscription.deadband_monitor()`, which also sets `Trigger = StatusValue` so a tag going Bad while steady still reports |
| **The SLDC scraper died on any database outage** | This test stops the database the scraper also writes to; `launchctl` showed `runs = 4, last exit code = 1` | See below |

## The scraper defect, and why it matters beyond the scraper

The scraper held one database connection for the life of the process with no
reconnect, and its error handler tried to log the failure *through the
connection that had just died*. The second exception escaped the poll loop and
killed the process. launchd's `KeepAlive` restarted it each time, so from
outside it looked like uptime. It had already crashed three times before anyone
looked.

Fixed: connections are acquired on demand, a broken one is discarded rather than
reused, recording a poll attempt is best-effort, and no single tick can take the
loop down.

Verified twice. Against a port with nothing listening: exit 0, zero tracebacks.
Then against this stage's real three-minute outage, with the fixed code running
under launchd:

```
16:48:01  recorder starting                      pid 71829, runs 5
16:49:01  could not record poll attempt (db_error) ... database write failed
16:50:01  could not record poll attempt (db_error) ... database write failed
16:51:01  could not record poll attempt (db_error) ... database write failed
16:52:01  ok: page 22/09/2026 22:15 (age 421s), 6 new rows, 6/6 stations good
```

`pid 71829` and `runs 5` unchanged across the outage: it did not restart, it
recovered. Zero tracebacks.

The general lesson is worth recording for the FAT report: a process supervisor
that restarts on failure will convert a crash into something that looks like
uptime. `runs` and `last exit code` are the figures that tell the truth, and
they are worth watching for every supervised process in the final system.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
