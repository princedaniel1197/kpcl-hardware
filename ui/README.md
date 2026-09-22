# ui — React visualisation (Stage 12)

```bash
make api      # FastAPI on :8000 — event stream and archive
make ui       # Vite dev server on :5173, proxying /api and /ws
```

Built last, deliberately. It hangs off real events from a working system; built
first it would have been a nice interface over nothing.

## Every particle is an event

Nothing in the pipeline view is on a timer. A dot appears because the collector
emitted `value_received`, and it reaches the historian because the collector
emitted `value_forwarded`. When the archive is unreachable the collector emits
`value_buffered` instead — so the dots stop at the buffer and the buffer fills,
not because the animation was told the network is down, but because that is
what the collector actually reported doing.

## The event stream does not go through the archive

The first design relayed events over Postgres `LISTEN/NOTIFY`: elegant, no new
listener, uses infrastructure already required. It was also wrong. The pipeline
view exists to show what happens when the **archive** becomes unreachable, and
routing the events through the archive freezes the picture at exactly the
moment it has something to say — the buffer would fill, the collector would keep
working perfectly, and the screen would show nothing.

So the collector serves its own stream (`ws://127.0.0.1:8090/events`) and the
API subscribes to it. An archive outage is now visible rather than invisible.
That socket sends and never receives: the collector still has no write path.

## Quality is visible, not smoothed

- A **Bad** value is a red square in transit, is stopped at the KPI node, and
  pulses there with the reason shown. It does not pass through.
- An **Uncertain** value is an amber circle.
- On a trend, a Bad sample **breaks the line** — `connectNulls={false}` is the
  whole difference between a chart that tells the truth and one that invents a
  straight line across a sensor failure. A `×` marks each Bad point so a break
  is distinguishable from "no data yet".
- On the mimic, a Bad value reads `- - -`, never `0`.

## The mimic follows ANSI/ISA-101.01-2015

Grey, low-saturation base; **colour reserved for abnormal states**. On a
conventional colourful mimic an alarm competes with the decoration; here,
anything coloured is the only thing coloured. Four-level display hierarchy: Unit
Overview and Unit TSI Overview are levels 2 and 3 (§469), and the layout is
identical across units (§470).

Live bindings: the turbine rotor turns at the **actual** speed tag (at 3000 rpm
it turns; at standstill it does not), the breaker draws open or closed, the load
bar fills to 210 MW.

The drum vessel shows **pressure**, not level, and says so on the display. This
simulator has no drum level instrument and drawing one would be inventing it.

## The time scrubber replays the archive

Not a recording of the animation — the samples themselves, with their original
source timestamps and quality. That distinction is the point: the outage and
recovery can be replayed *because* the data is there, which is the claim the
whole system is making.
