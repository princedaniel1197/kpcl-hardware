# ui — React visualisation (Stage 12)

```bash
make api      # FastAPI on :8000 — event stream and archive
make token    # a read-only access token, shown once
make ui       # Vite dev server on :5173, proxying /api and /ws
```

Every API call is authenticated (§509), so the first screen asks for a token.
It is kept in `localStorage` until **Sign out** or until the API refuses it
(revoked or expired) — so anyone using that browser profile can open the
dashboard — and is sent as a bearer header, and on the WebSocket as a
subprotocol; it never appears in a URL. Revoke a token with
`python -m ops.access revoke NAME`.

Built last, deliberately. It hangs off real events from a working system; built
first it would have been a nice interface over nothing.

## Every move a particle makes is an event

A dot appears at the DCS because the collector emitted `value_received`, and
travels as far as the collector. It goes on to the historian only when the
collector reports `value_forwarded`; it stops at the buffer when the collector
reports `value_buffered`; it leaves the buffer when the collector reports the
drain. Batch events are matched to dots oldest-first, the order the collector
sends in. So when the archive is unreachable the dots pile up at the buffer —
not because the animation was told the network is down, but because that is
what the collector actually reported doing.

(Until 23 September the header of `Pipeline.jsx` said this while the code
animated every received value all the way on a timer and only reacted to
`value_buffered`. The code now does what the comment said.)

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
  straight line across a sensor failure. A dashed red vertical line marks each
  Bad sample, at its instant and at no particular value (an earlier version
  marked it at y = 0, which is a picture of a zero).
- The trend's x-axis is **time**, so a stretch with no data — the archive
  unreachable — is blank space that fills in from the left as the buffer
  drains, rather than disappearing between evenly spaced points.
- On the mimic, a Bad value reads `- - -`, a digital with no known state reads
  `?`, and a bar or vessel with no value is hatched. (An earlier version drew a
  Bad speed as "0 rpm", a Bad breaker as OPEN and a Bad load as an empty bar.)

## The mimic follows ANSI/ISA-101.01-2015

Grey, low-saturation base; **colour reserved for abnormal states**. On a
conventional colourful mimic an alarm competes with the decoration; here,
anything coloured is the only thing coloured. Of the standard's four-level
display hierarchy, levels 2 and 3 are built — Unit Overview and Unit TSI
Overview (§469). Level 1 (plant overview) and level 4 (diagnostic detail) are
not. The mimic takes a unit's tag prefix and asset code and nothing else, so
every unit is drawn by the same code in the same places (§470).

Live bindings: the turbine rotor turns at the **actual** speed tag (at 3000 rpm
it turns; at standstill it does not; with no value it is drawn dashed and still),
the breaker draws open, closed or unknown, the load bar fills to the rated 210 MW.

The KPI strip and the KPI node read values the engine service computes
continuously (`python -m engine`); before 23 September nothing ran it, and they
showed whatever the last demonstration script had left.

The drum vessel shows **pressure**, not level, and says so on the display. This
simulator has no drum level instrument and drawing one would be inventing it.

## The time scrubber replays the archive

Not a recording of the animation — the samples themselves, with their original
source timestamps and quality. That distinction is the point: the outage and
recovery can be replayed *because* the data is there, which is the claim the
whole system is making.
