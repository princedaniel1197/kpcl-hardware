# Stage 6 — Quality rules and tag health

| | |
|---|---|
| Stage | 6 — Quality rules and tag health |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 6 |
| Acceptance criterion | Force each of the four conditions. Each is flagged, distinguishable from source quality, and visible downstream. |
| Date performed | 2026-09-22 |
| Method | `python -m engine.quality_demo` against the live simulator and collector |
| **Result** | **PASS** — 15 of 15 checks |

## Each condition, forced on the live system

| Condition | How it was induced | Flagged as | Source quality |
|---|---|---|---|
| Out of range | EURange narrowed to 0–300 °C on a 538 °C process — a mis-scaled transmitter | `BadOutOfRange` (2151415808) | stayed **Good** |
| Rate of change | start-up restarted: full load to cold in one scan | `UncertainSensorNotAccurate` — "rate 998.7/s exceeds limit 60/s" | stayed **Good** |
| Cross-tag | consistency tolerance tightened past the real agreement | `UncertainSubNormal` — "670.012 differs from U1_MW × 3.19 = 671.294 by 0.2%" | stayed **Good** |
| Frozen | `U1_MW` pinned at exactly 0 with the breaker open | `UncertainLastUsableValue` — "unchanged within 0.001 for 30s, 4 samples, spread 0" | stayed **Good** |
| **Arrived Bad** (control) | forced through the simulator's control API | health `is_bad` | **BadDeviceFailure** (2156593152) |

## The distinction the stage exists to prove

```
  arrived Good, failed range : source=0            computed=2151415808
  arrived Bad                : source=2156593152   computed=1083375616
```

A value the instrument stands behind and the system doubts, and a value the
instrument disowns, are different facts and stay different facts. `sample.quality`
holds the StatusCode exactly as acquired and is never overwritten; computed
verdicts live in `quality_flag`, and the `sample_quality` view shows both
columns side by side.

Severity is assigned deliberately, not conveniently. Out of range and stale are
**Bad** — the first is outside what the instrument can represent, the second has
no measurement to judge. Rate of change, cross-tag and frozen are **Uncertain** —
the reading may be perfectly real and what is in doubt is whether to believe it.
Calling those Bad would discard data that is probably fine.

## Two defects found, one of them serious

### 1. A deadband was hiding every quality change (critical)

With a server-side deadband configured, forcing a tag to `BadDeviceFailure`
produced **zero** Bad notifications. Without one, the notification arrived
immediately. Measured directly against the simulator:

```
  deadband_monitor(1.8)                 notifications after forcing Bad: 1, Bad seen: 0
  subscribe_data_change (no deadband)   notifications after forcing Bad: 6, Bad seen: 1
```

The cause is in asyncua 2.0.1's server, `monitored_item_service.py`:

```python
deadband_flag_pass = self._is_data_changed(mdata.mvalue, mdata.filter.Trigger) \
                     and self._is_deadband_exceeded(mdata.mvalue, mdata.filter)
```

The trigger and the deadband are **ANDed**. A change of StatusCode with the
value unchanged passes the trigger and fails the deadband, so it is never sent.
Per OPC UA Part 4 a deadband governs *value* changes; a `StatusValue` trigger
should report a status change regardless.

This sat in the acquisition path of a system whose central claim is that quality
is never lost. Stage 3's outage test would not have caught it: nothing forced a
quality change during that test, and every sample that *was* produced arrived
intact.

**Fixed** by removing the server-side deadband from the acquisition path
entirely. The deadband's purpose — not archiving values that carry no
information — is served instead by Stage 4's compression, which forces an
archive on a quality change by construction.

**The cost, measured** (§317 requires the load on the source to be measured, not
assumed), over 60 s across 14 tags at a 500 ms publishing interval:

| | notifications | rate |
|---|---|---|
| with server-side deadband | 601 | 10.0/s |
| without (what is now used) | 1334 | 22.2/s |
| increase | | **2.2×, +12.2/s** |

A 2.2× increase in notifications from the source, in exchange for never losing a
quality change. At demonstrator scale that is 12 extra notifications per second.
At the tendered 34,700 I/O it would need re-measuring, and the honest answer is
that this project has not measured it at that scale.

### 2. Frozen and stale were the same observable

`U1_MW` pinned at exactly 0 reported **missing**, not frozen. An OPC UA
subscription reports on change, so a stuck transmitter produces *silence*, not a
stream of identical values — and §439 requires "frozen" and "stale" to be
distinguishable, because a tag that is alive but stuck is a different fault from
one whose link has failed.

**Fixed** by adding a periodic max-time read to the collector: a tag not heard
from for its `max_time_ms` is read. This is an ordinary OPC UA **read** — the
session stays read-only — and the DataValue it returns carries the server's own
SourceTimestamp. Nothing is re-stamped. The alternative, re-recording the last
known value under a fresh timestamp, would be exactly the fabrication §335
forbids.

With a 10 s max-time on `U1_MW`, the archive then shows:

```
 2026-09-22 17:32:48.629529+00 |     0 |       0
 2026-09-22 17:32:38.629618+00 |     0 |       0
 2026-09-22 17:32:28.63069+00  |     0 |       0
 2026-09-22 17:32:18.630565+00 |     0 |       0
```

Four real reads, 10 s apart, each with its own measurement time — and the frozen
rule now has something to detect.

### 3. The frozen rule could never fire

Found while chasing the above. The rule filtered samples to the last `window`
seconds and then required that slice to **span** `window` — which that filter can
never produce. It also could not distinguish "unchanged for five minutes" from
"we only hold twenty seconds of history", which are different claims.

Rewritten to take timestamped samples and to refuse to conclude when the history
does not reach back across the window. `test_frozen_will_not_claim_more_than_the_history_supports`
covers it.

## Tests

`pytest engine/test_quality.py -q` → **24 passed**, covering all five rule
functions, the severity assignments, and the two distinctions that matter:
arrived-Bad versus failed-a-check, and missing versus stale.

A fourth correction was to a test, not the code: `test_frozen_tolerance...`
asserted that a larger tolerance flags less readily. The opposite is true — the
tolerance is how much movement still counts as not moving — and the test was
asserting the inverse of the rule's semantics.

## Deviation from the build plan

The plan suggests feedwater flow against **main steam flow** as the cross-tag
pair. The simulator has no main steam flow tag, so the equivalent real
relationship available is feedwater flow against gross generation: steam flow
tracks load, and at 210 MW the unit takes about 670 t/h, giving ~3.19 t/h per MW.

It is **gated on load**, because the relationship holds in a regime rather than
always: during a cold start-up the boiler is being filled with the breaker open,
and an ungated rule would fire through every start-up. A check that cries wolf
through every normal evolution gets switched off, so the regime is part of the
rule.

## Addendum — 23 September 2026

- **The out-of-range condition was forced without an audit row.** The
  demonstration narrowed U1_MS_TEMP's EURange with a direct UPDATE and restored
  it the same way, so `audit_log` never showed the range had been anything but
  0–600 °C — and the 1,251 range flags it left looked, in the FAT report, like
  a defect in the tag configuration. It now changes configuration through
  `archive/audit.py` and restores in a `finally`.
- Two of the fifteen checks tested a constant (`severity(RATE_EXCEEDED) == 1`)
  rather than the flag the rule produced. They now test the flag.
- Rate of change is flagged plain `Uncertain` from 23 September.
  `UncertainSensorNotAccurate`, used above, says the value is at a sensor
  limit, which the rule does not know.
- The "no server-side deadband" decision is now per server
  (`config/sources.json`), on by default. Re-measured on 23 September: with the
  deadband at the source, 6.8 notifications/s and **0** Bad notifications for a
  forced tag; without, 22.0/s and the Bad notification arrived. The 2.2× cost
  quoted above was measured during a start-up; at steady load it was 3.2×.
- Until 23 September nothing ran these rules except this demonstration. The
  engine service (`python -m engine`) now evaluates them every 10 seconds.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
