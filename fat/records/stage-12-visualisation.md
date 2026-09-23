# Stage 12 — The visualisation

| | |
|---|---|
| Stage | 12 — The visualisation |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 12 |
| Acceptance criterion | A colleague who has not seen it can watch the outage and recovery and describe what happened without being told. |
| Date performed | 2026-09-22 |
| **Result** | **BUILT and functionally verified. The acceptance criterion is a human judgement and has NOT been obtained.** |

## Why this record does not say PASS

The criterion names a person: *a colleague who has not seen it*. Nobody has
watched it. Everything below is what was measured — the behaviour is correct and
the outage is legible to me — but I wrote it, so I am the last person whose
opinion of its legibility is worth anything.

That test is the one thing in this stage that needs someone else, and it is
recorded as outstanding rather than quietly satisfied.

## The outage, as the screen showed it

Archive stopped at 19:16:36. After 25 seconds, with the collector still
acquiring:

| Node | Showed |
|---|---|
| BUFFER | **707**, "holding — uplink down", red border |
| NETWORK | **DOWN**, "consuming input failed: server closed the connection" |
| link | dashed red between buffer and network |
| particles | stopped at the buffer |

Archive restarted at 19:17:10. Within seconds:

| Node | Showed |
|---|---|
| BUFFER | **0**, "empty" |
| NETWORK | **UP**, "archive reachable" |
| legend | "last drain: 1010 samples in 0.08s" |

The collector's own log for the same period:

```
19:16:36  archive link down: consuming input failed: server closed the connection
19:17:12  archive link up: reconnected
19:17:12  draining 1010 buffered samples in source-timestamp order
19:17:13  drained 1010 samples in 0.1s; buffer now 0
```

The screen and the log agree because they are the same events.

## Every particle is an event

Nothing is on a timer. A dot appears because the collector emitted
`value_received`; it reaches the historian because the collector emitted
`value_forwarded`. When the archive is unreachable the collector emits
`value_buffered` instead, so the dots stop and the buffer fills — not because
the animation was told the network is down, but because that is what the
collector reported doing.

## The defect that mattered most

The first design relayed events through Postgres `LISTEN/NOTIFY`. Elegant: no
new listener, uses infrastructure already required to be running.

It was also wrong, in a way that would have been embarrassing in front of KPCL.
**The pipeline view exists to show what happens when the archive becomes
unreachable, and the events were being routed through the archive.** During the
outage the collector would keep working perfectly, the buffer would fill, and
the screen would show nothing at all — frozen at the exact moment it had
something to say.

Fixed by having the collector serve its own stream over WebSocket, which the API
subscribes to. No database in the path. That socket sends and never receives:
the collector still has no write path of any kind.

A second, smaller one: the particle overlay kept its own SVG coordinate system
while React Flow's `fitView` transformed the nodes, so every particle floated
above the diagram instead of riding the edges. The flow is now pinned at zoom 1
and the whole canvas is scaled by CSS, so a node at x=410 is at 410 px in both.

## Quality is visible, never smoothed

| Where | Bad value |
|---|---|
| In transit | red **square** — shape as well as colour, so it survives a colour-blind viewer and a projector |
| At the KPI node | stopped and pulsing, with the reason: "input HubTemperature (RIG_HUB_TEMP) has no value, BadDeviceFailure" |
| On a trend | the line **breaks**; `×` marks the point |
| On the mimic | `- - -`, never `0` |

`connectNulls={false}` on the trend is the entire difference between a chart
that tells the truth and one that draws a straight line across a sensor failure.

**After recovery the trend has no gap**, because the buffered samples backfilled
with their original source timestamps. That is the Stage 3 result made visible.

## The mimic follows ANSI/ISA-101.01-2015

Grey low-saturation base, **colour reserved for abnormal states**, four-level
hierarchy with Unit Overview and Unit TSI Overview (§469) on an identical
layout (§470). Live bindings: the rotor turns at the actual speed tag, the
breaker draws open or closed, the load bar fills against 210 MW.

The drum vessel shows **pressure, not level**, and says so on the face of the
display. This simulator has no drum level instrument and drawing one would be
inventing an instrument.

## The time scrubber

Replays the archive — 4,251 samples with their original source timestamps and
quality — at 0.5× to 20×, not a recording of the animation. The distinction is
the point: the outage can be replayed *because* the data is there.

## To close this record

Sit someone in front of it who has not seen it. Run `make sim`, `make collector`,
`make api`, `make ui`. Stop the database with `docker stop crpms-timescaledb`,
wait a minute, start it again. Ask them what happened. Write down what they say,
in their words, and whether they needed prompting.

If they cannot describe it, the display is wrong, not the viewer.

## Addendum — 23 September 2026

Three things this record describes were not true of the code, and are now:

- **"Every particle is an event."** The code animated every received value to
  the end of the pipeline on a timer and reacted only to `value_buffered`. Dots
  now move only when the collector reports `value_forwarded`, `value_buffered`
  or the drain.
- **The mimic showed Bad values as zero in its graphics**: a Bad speed read
  "0 rpm", a Bad breaker drew OPEN, a Bad load drew an empty bar. Unknown is now
  drawn as unknown.
- **The trend's `×` was drawn at y = 0**, which is a picture of a zero, and its
  categorical axis hid a stretch with no data. Bad samples are now a vertical
  line at their instant, on a real time axis.

Also: every API call needs a token, so the display opens on a sign-in; the KPI
strip reads values the engine service computes continuously (it showed
twelve-hour-old values before); and of ISA-101's four display levels, two are
built (§469). The criterion — a colleague describing an outage unaided — has
still not been attempted.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
