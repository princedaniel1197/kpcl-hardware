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

## A module of Sentinel

Since 24 September 2026 CRPMS looks like a module of Sentinel
(https://kpcl.vercel.app): the same ivory ledger, shell and components, taken
from Sentinel's own source (`princedaniel1197/KPCL`, branch `sentinel-v2`, which
is what the live site serves) rather than re-drawn from screenshots.

- **One tokens file**, `src/styles/sentinel.css`: Sentinel's `globals.css`
  unchanged, then a short block of CRPMS additions. Tailwind carries the same
  palette (`tailwind.config.js`). Checked against the live site's computed
  styles: DM Sans 14 px/21 px body, Cormorant Garamond 600 headings, ink
  `#2A2418` on paper `#F5F1E8`, gold `#C9A84C` rules.
- **No monospace face.** Sentinel has none — its numbers, tags and timestamps
  are DM Sans with tabular figures — so neither does CRPMS. Tag-name titles
  take Cormorant's lining figures, or "U1" reads "UI".
- **Fonts are self-hosted** (`@fontsource`), as Sentinel's are through
  `next/font`: no request to Google at runtime.
- **The shell** (`src/components/Shell.jsx`) is Sentinel's: the 236 px ruled
  sidebar with grouped navigation, the sticky header with a station and a
  period selector and search, the drawer below 1024 px. Where Sentinel's header
  ends with an English/ಕನ್ನಡ toggle, CRPMS's ends with the state of the live
  stream: CRPMS's strings are not translated, and a toggle that changed nothing
  would be a false control.
- **The components** (`src/components/ui.jsx`) are Sentinel's `PageHeader`,
  `Section`, `Kpi`, `Ledger`, `Chip`, `ProvenanceChip`, `Note`, `Folio` and
  print bar, plus what CRPMS adds: `QualityChip`, `Value` and `Time`.
- **Print**: Sentinel's print CSS. On paper CRPMS drops the sidebar and header
  and takes the whole sheet; Sentinel prints its sidebar, which is the one
  place the two deliberately differ.

Pages: Overview · Asset tree and element folios · Tag register and tag folios ·
Unit overview (mimic and TSI) · Trends · Replay · Bench rig · Event frames ·
KPI register with lineage · Collector & buffer (the pipeline) · Gaps & losses ·
Source health · Data sources · Settings. The station and period ride in the
query string across every link, as Sentinel's do.

## On Vercel

Vercel builds this directory on every push to `main` (`vercel.json` at the
repository root; `.vercelignore` keeps everything but `ui/` and `config/` out,
so the Python `api/` is never deployed as a function). What it hosts is the
interface alone: the API, collector, engine and archive run on the station
laptop (`make start`), and the hosted copy has no data behind it. Signing in
there says that the API did not answer, rather than showing an empty ledger.

## Every value carries its quality, time and provenance

Wherever a value appears it shows the number and unit, a quality chip (Good,
Uncertain, Bad — the numeric StatusCode decoded by name, from
`src/lib/statusCodes.js`, generated from asyncua's table by
`scripts/gen_status_codes.py`), the source timestamp (IST, with UTC on hover),
and a provenance chip: **Real** for the bench rig's `RIG_*` tags and the
collector's own measurements, **Synthetic** for the DCS simulator, the load
test and the OMF demonstration. A **Bad** value is "—" with its reason, never a
stale or zero number; an **Uncertain** value keeps its number, in amber, with
its reason — for `RIG_CURRENT`, "uncalibrated - bench demo only". Colour
signals state and nothing else: red Bad, amber Uncertain, green asserted-good.

`npm test` checks these rules on the rendered components (`tests/values.test.jsx`)
and would fail if a Bad value rendered a number, an Uncertain value lost its
number or its note, a StatusCode decoded to the wrong name, or provenance were
assigned against the rule. The dashboard's poll stays in `src/App.jsx` as
`setInterval(poll, 2500)`, which is what FAT test T-03 reads.

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

A quiet base — now Sentinel's ivory and ink rather than grey — and **colour
reserved for abnormal states**. On a
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
