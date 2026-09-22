# CRPMS Demonstrator — Build Plan

A miniature but architecturally complete plant monitoring system. Everything in the
tendered KPCL CRPMS except scale.

**How to use this file.** Drop it in the repository root. Work one stage at a time.
Each stage gives you a prompt to hand Claude Code, the design decisions that must not
be delegated, and a test that decides whether the stage is done. Do not start a stage
until the previous one's test passes.

**The rule that makes this worth doing.** Every stage below exists because a specific
clause of the KPCL scope of work demands it. The clause numbers are real. When you
demo this, you are not showing features — you are showing compliance, in advance.

---

## Stage 0 — Environment

```
orianode-crpms/
  sim/          OPC UA server standing in for a DCS
  collector/    acquisition, buffering, compression
  archive/      schema, migrations
  engine/       quality rules, KPIs, event frames
  api/          FastAPI + WebSocket
  ui/           React visualisation
  fat/          test plan and records
  firmware/     ESP32
```

Install: Python 3.11+, Docker, Node 20+.
Python packages: `asyncua pymodbus psycopg[binary] fastapi uvicorn pydantic numpy`

**Prompt for Claude Code:**
> Set up a Python project at ./ with the directory structure above, a pyproject.toml
> using the packages listed, a docker-compose.yml running TimescaleDB (latest-pg16) on
> port 5432 with a named volume, and a Makefile with targets: up, down, sim, collector,
> api, ui, test. Use asyncio throughout. No web framework other than FastAPI.

**Done when:** `make up` starts TimescaleDB and `psql` connects.

---

## Stage 1 — The DCS simulator

Stands in for BHEL maxDNA / Yokogawa CENTUM. Everything downstream talks to this, so
it has to behave like the real thing, not like a convenient test fixture.

**Design decisions — do not let these be guessed:**

- `SourceTimestamp` is set when the value is *produced*. `ServerTimestamp` is set when
  it goes on the wire. They must differ. If they are identical on a changing tag, the
  implementation is wrong. (§335)
- Quality is a real `StatusCode`, not a boolean or a string. Use `Good`,
  `BadDeviceFailure`, `UncertainSensorNotAccurate`. (§318)
- Every tag carries engineering units and an EURange. (§436)
- Digitals change state; they are not sampled analogues. (§440)

**Prompt for Claude Code:**
> Build an OPC UA server in sim/ using asyncua that models one 210 MW thermal unit.
> Expose these tags with engineering units and EURange: U1_MW (MW), U1_TURB_SPEED (rpm),
> U1_DRUM_PRESS (kg/cm2), U1_MS_TEMP (degC), U1_MS_PRESS (kg/cm2), U1_FEEDWATER_FLOW (t/h),
> U1_COAL_FLOW (t/h), U1_AUX_POWER (MW), U1_CONDENSER_VAC (mmHg), U1_GEN_STATOR_TEMP (degC),
> U1_BEARING_VIB (mm/s), and digitals U1_BOILER_LIGHTUP, U1_TURB_ROLLING, U1_BREAKER_CLOSED.
>
> Drive them through a cold start-up: light-up, pressure raise, turbine rolling to 3000 rpm,
> synchronisation, load ramp to 210 MW, then steady operation with realistic noise. Make the
> phase timings configurable so a full start-up can run in 90 seconds for demos or 3 hours
> for realism.
>
> Write each value as a ua.DataValue carrying Value, StatusCode_, SourceTimestamp and
> ServerTimestamp. SourceTimestamp must be the instant the simulator computed the value;
> ServerTimestamp must be set separately at write time. They must not be equal.
>
> Expose a small HTTP control API on a separate port to force any tag to
> BadDeviceFailure or UncertainSensorNotAccurate, and to restore it to Good. This is how
> we will induce faults during the demo without touching the simulator's logic.

**Test:** an asyncua client reads U1_MW ten times and prints value, StatusCode name,
SourceTimestamp and ServerTimestamp. The two timestamps differ on every sample. Forcing
Bad through the control API changes the StatusCode within one scan.

---

## Stage 2 — Schema and archive

**Design decisions:**

- A sample row is `(tag_id, source_ts, server_ts, value, quality)`. Four of those five are
  not optional. Most naive schemas store value and one timestamp and lose the argument
  before it starts. (§335, §460)
- The primary key is `(tag_id, source_ts)`. This is what makes backfill idempotent —
  replaying a buffered value can never create a duplicate. (§382, §648)
- Asset framework is relational: templates, elements, attributes. (§341, §392)

**Prompt for Claude Code:**
> Create the TimescaleDB schema in archive/ as numbered SQL migrations.
>
> Tables:
> - tag: id, name, description, engineering_unit, range_low, range_high, source_system,
>   scan_rate_ms, exc_dev, comp_dev, element_id
> - sample: tag_id, source_ts, server_ts, value double precision, quality smallint —
>   hypertable on source_ts, PRIMARY KEY (tag_id, source_ts)
> - element_template, attribute_template, element (self-referencing parent_id), attribute
> - event_frame: id, template, element_id, start_ts, end_ts, status
> - event_milestone: event_frame_id, name, ts, value
> - kpi_definition: id, name, classification, equation, inputs jsonb, constants jsonb,
>   validity_low, validity_high, bad_data_treatment, version, valid_from, valid_to
> - kpi_value: kpi_definition_id, kpi_version, element_id, ts, value, quality, reason
> - audit_log: id, ts, actor, entity, entity_id, field, old_value, new_value, reason
>
> Add a retention policy of 10 years on sample and a continuous aggregate giving 1-minute
> averages. Index for the query "all samples for one tag over 24 hours" and prove it
> returns in under 5 seconds with 10 million rows.
>
> Quality is stored as the numeric OPC UA StatusCode, not a string. Provide a helper
> view that decodes it to Good/Bad/Uncertain.

**Test:** insert 10 million synthetic rows; a 24-hour single-tag query returns in
under 5 seconds (§528). Inserting the same (tag_id, source_ts) twice does not duplicate.

---

## Stage 3 — The collector

This is the heart of the whole exercise. Everything else is ordinary software. Spend
your attention here.

**Design decisions — these are what you will be asked about:**

- Subscribe with per-tag deadbands; do not poll everything at maximum rate. The load you
  place on the source is a measured quantity, not an assumption. (§317)
- The session is **read-only**. There is no write method anywhere in this module. That is
  an architectural guarantee, not a promise. (§303, §315)
- On forward failure: buffer locally, keep acquiring. Loss of the uplink must not stop
  acquisition. (§319)
- On restore: replay in chronological order, preserving the original SourceTimestamp and
  quality, with an upsert that cannot duplicate. (§382, §648)
- Gap detection by sequence number, so a genuine gap is detectable rather than inferred.

**Prompt for Claude Code:**
> Build the collector in collector/. It subscribes to the OPC UA server from Stage 1 using
> asyncua, with a per-tag deadband and sampling interval read from the tag table.
>
> Requirements, in order of importance:
> 1. The OPC UA session must be read-only. Do not import or implement any write path.
>    Add a unit test that asserts no call to write_value exists in this package.
> 2. For every received DataValue, extract value, StatusCode, SourceTimestamp and
>    ServerTimestamp. Never substitute receipt time for a valid SourceTimestamp.
> 3. Forward to TimescaleDB with INSERT ... ON CONFLICT (tag_id, source_ts) DO NOTHING.
> 4. If the forward fails, write to a local SQLite buffer and keep subscribing. Buffer
>    depth is bounded and configurable; log when it exceeds 80 percent.
> 5. On reconnection, drain the buffer in ascending source_ts order before resuming live
>    forwarding, so the archive never receives out-of-order data during recovery.
> 6. Maintain a monotonic sequence number per tag; on restore, detect and log any gap.
> 7. Publish collector health as tags in the same archive: link state, buffer depth,
>    samples per second, last successful forward, CPU and memory. (§462)
>
> Expose an asyncio event stream that emits a small JSON event for every meaningful
> occurrence — value received, value buffered, buffer drained, gap detected, quality
> changed. The visualisation in Stage 12 consumes this. Do not build the visualisation now.

**Test — the one that matters.** Start everything. Let it run five minutes. Stop
TimescaleDB. Watch the buffer grow. Wait three minutes. Start TimescaleDB. Then verify:
no sample missing between the stop and start instants; every restored row carries its
original source_ts and quality; zero duplicates; the trend has no gap. Write the result
down — this becomes the first entry in your FAT record.

---

## Stage 4 — Compression

**Design decisions:**

- Two stages, as PI does it: exception reporting on `ExcDev` first, then swinging-door
  compression on `CompDev`. Convention is `ExcDev ≈ CompDev / 2`. (§442)
- Always store regardless of deadband when: quality changes, the timestamp goes backwards,
  the value is non-numeric, or maximum time since last archive is exceeded. These four
  exceptions are where naive implementations lose data that matters.

**Prompt for Claude Code:**
> Implement two-stage compression in collector/compression.py. Stage one is exception
> reporting against exc_dev with a maximum time limit. Stage two is the swinging-door
> algorithm against comp_dev, following Bristol's method (US4669097A): maintain upper and
> lower slope bounds from the last archived point, archive the previous point when a new
> point falls outside the corridor, then reset the corridor.
>
> Force an archive, bypassing both stages, when quality changes, when the timestamp is not
> greater than the previous one, or when max_time_ms has elapsed.
>
> Report compression ratio per tag. Provide a function that reconstructs the original
> series by linear interpolation between archived points, and a test asserting the
> reconstruction error never exceeds comp_dev.

**Test:** on a slow tag like U1_MS_TEMP, compression ratio exceeds 10:1 and maximum
reconstruction error stays within comp_dev. On a noisy tag like U1_BEARING_VIB the ratio
is low — which is correct, and worth being able to explain.

---

## Stage 5 — Asset framework

**Prompt for Claude Code:**
> Build the asset model in engine/assets.py against the Stage 2 tables. Create base
> templates and derived templates. Build the hierarchy KPCL → Station → Unit → System →
> Sub-system → Equipment → Component → Parameter (§392). Each element carries a unique
> permanent asset code that is never reused.
>
> Seed it with: KPCL → RTPS → Unit 1 → Boiler / Turbine / Generator → equipment →
> parameters, mapping the Stage 1 tags onto attributes. Name tags and elements to
> ISA-95/ISA-88 principles and document the convention in a short README (§385).
>
> Provide a query function that answers "give me this attribute across every element of
> this template" — for example every bearing temperature on every unit. That single
> capability is what an asset framework is for.

**Test:** adding a second simulated unit requires creating one element from a template,
not re-mapping tags by hand.

---

## Stage 6 — Quality rules and tag health

**Prompt for Claude Code:**
> Implement the four data-quality rules from §384 in engine/quality.py, with per-tag
> configurable thresholds: range check against the tag's EURange, rate-of-change check,
> cross-tag consistency check (define at least one real pair, for example feedwater flow
> against main steam flow within a tolerance band), and frozen or stale value detection.
>
> Separately implement §439 tag health, exposing per tag whether it is currently bad,
> stale, frozen, missing, out of range, or communication-failed.
>
> Computed quality must be distinguishable from source quality. A value that arrived Good
> but failed a range check is not the same thing as a value that arrived Bad, and the
> system must be able to say which.

**Test:** force each of the four conditions. Each is flagged, distinguishable from source
quality, and visible downstream.

---

## Stage 7 — KPI engine

**Design decision:** a KPI with a Bad input does not return zero, does not return the last
good value, and does not return a number with a footnote. It returns Bad, with the reason
attached. This is §318 at the calculation layer and it is the single most demonstrable
difference between a team that understands OT and a team that does not.

**Prompt for Claude Code:**
> Build the KPI engine in engine/kpi.py. Each KPI is a row in kpi_definition carrying its
> equation, input tags, constants, engineering units, reference values, validity range,
> calculation frequency, bad-data treatment, and a version. Every computed kpi_value stores
> the definition version used, so any historical result can be traced to the exact equation
> that produced it (§320, §379, §381).
>
> Classify each KPI as measured, calculated, derived/statistical or AI/ML and expose the
> classification (§378).
>
> Implement at least: unit heat rate, auxiliary power consumption percentage, and specific
> coal consumption.
>
> Quality propagation rule: if any input is Bad, the KPI is Bad and carries a reason naming
> the offending input. If any input is Uncertain, the KPI is Uncertain. If a result falls
> outside its validity range, it is flagged. Never silently substitute.
>
> Generate the KPI dictionary as a markdown document from the database (§383).

**Test:** force U1_COAL_FLOW to Bad through the Stage 1 control API. Heat rate goes Bad
within one calculation cycle, naming coal flow as the cause. It does not go to zero.

---

## Stage 8 — Event frames

**Prompt for Claude Code:**
> Build event frame detection in engine/events.py as a state machine over live tags.
>
> Detect a thermal start-up with these milestones (§472): boiler light-up, steam admission,
> turbine rolling, synchronisation, loading, and for shutdown, coast-down. Each trigger has
> a condition and a "true for N seconds" debounce so that a spike does not start an event.
>
> For each event frame store start and end timestamps, every milestone timestamp, and the
> minimum, maximum and mean of a configured set of tags over the window.
>
> Implement comparison (§475): this event against a stored reference curve, and against the
> previous best event of the same template. Report deltas per milestone — for example
> "synchronisation reached 4 minutes 12 seconds later than best".
>
> Provide a query returning all event frames for an element with their milestone tables.

**Test:** run a simulated start-up. The frame is captured automatically with all
milestones. Run a second, slower one. The comparison reports the delta per milestone.

---

## Stage 9 — OMF emitter

This is the bridge to a real PI system and the best answer you have to "or equivalent".

**Prompt for Claude Code:**
> Add an OMF output path to the collector, following the AVEVA OMF specification
> (github.com/AVEVA/OMF-Docs, Apache-2.0). Emit the three message kinds — Type, Container,
> Data — as JSON over HTTP.
>
> Build a small OMF receiver in archive/ that accepts those messages and writes to
> TimescaleDB, so the collector's only output format is OMF.
>
> The endpoint must be configurable, so that pointing the collector at a real PI Web API
> OMF endpoint requires a URL and credentials and no code change. Document that in the
> README.

**Test:** messages validate against the OMF schema. Changing one config value switches the
destination.

---

## Stage 10 — The hardware rig

**Prompt for Claude Code (firmware):**
> Write ESP32 firmware in firmware/ using the eModbus library (MIT) exposing a Modbus TCP
> server. Map: ACS712 current on input register 0, MPU-6050 vibration RMS on 1, DS18B20
> temperature on 2, and a digital run/stop state on discrete input 0. Scale to engineering
> units and document the register map.
>
> When the DS18B20 read fails — the probe is unplugged — the firmware must signal that
> condition distinctly rather than returning zero or the last value. Use a Modbus exception
> response or a dedicated status register, and document the choice.

**Prompt for Claude Code (bridge):**
> Write a Modbus-to-OPC-UA bridge that reads the ESP32 over pymodbus and republishes into
> the Stage 1 OPC UA server's address space as a second unit, mapping the failure signal
> onto StatusCode BadDeviceFailure. The collector must require no change.

**Test:** unplug the temperature probe. Within one scan the tag goes Bad in the archive,
the heat-rate equivalent KPI using it goes Bad, and the dashboard shows why.

---

## Stage 11 — Redundancy

**Prompt for Claude Code:**
> Run two collector instances against the same source and archive. Deduplication is already
> guaranteed by the (tag_id, source_ts) primary key, so both may write freely. Add health
> tags distinguishing the two, and a simple leader indication for reporting.
>
> Add a test that kills the primary mid-run and asserts zero missing samples across the
> transition (§455, §649).

**Test:** kill the primary collector during a start-up event. The event frame is still
complete.

---

## Stage 12 — The visualisation

Build this last. It hangs off real events from a working system; built first, it would be
the thing you rightly objected to — a nice interface over nothing.

**Prompt for Claude Code (API):**
> Add a FastAPI WebSocket endpoint in api/ that streams the collector's event stream to the
> browser, plus REST endpoints for tag lists, trends, asset hierarchy, KPI values and event
> frames.

**Prompt for Claude Code (pipeline view):**
> Build a React app in ui/ using React Flow showing the data path as nodes: DCS → Collector
> → Buffer → Network → Historian → KPI Engine → Dashboard.
>
> Animate individual values as particles moving along the edges, driven by real WebSocket
> events, not a timer.
>
> When the network node fails: particles stop at the buffer, the buffer node fills
> visibly, and the trend chart stops with a visible gap. On restore: the buffer drains
> in order, particles flow through, and the trend fills the gap in from the left while
> the viewer watches.
>
> A Bad-quality value must be visually distinct in transit and must be seen to be rejected
> at the KPI node with its reason shown, rather than passing through.
>
> Add a time scrubber so the outage and recovery can be replayed at any speed.

**Prompt for Claude Code (mimic):**
> Build an SVG unit mimic following ANSI/ISA-101.01-2015 high-performance HMI conventions:
> grey low-saturation base, colour reserved for abnormal states, four-level display
> hierarchy. Bind live tags to the graphics — turbine rotor rotating at actual speed, drum
> level filling, breaker opening and closing, load bar. Build Unit Overview and Unit TSI
> Overview displays (§469) and keep the layout identical across units (§470).

**Test:** a colleague who has not seen it can watch the outage and recovery and describe
what happened without being told.

---

## Stage 13 — The remaining named requirements

One evening each. All ordinary software; none needs domain knowledge.

- Role-based access: corporate, station, engineering, operations, maintenance, admin (§509)
- Audit trail on every configuration change: actor, timestamp, old value, new value, reason (§433)
- Automatic backup, restore to a clean environment, documented RPO and RTO (§512–518, §651)
- Configurable alerts with email delivery (§507)
- Central capacity monitoring and alerting (§344)
- Machine-readable export of data, KPI results and configuration (§503)

---

## Stage 14 — The FAT

The test report is the deliverable that persuades. The software is what makes the report
true.

**Prompt for Claude Code:**
> Create fat/ containing an Inspection and Test Plan and a FAT procedure. One row per test:
> reference clause, test condition, method, numeric acceptance criterion, expected result,
> actual result, pass or fail, signature. Include hold and witness points.
>
> Write an automated runner that executes every test that can be automated, captures
> evidence, and produces a signed-off markdown report with results and timestamps.

Tests to include, with the criteria from the scope of work:

| Clause | Test | Criterion |
|---|---|---|
| §528 | Acquisition to historian latency | P95 ≤ 5 s over ≥1000 samples |
| §528 | 24-hour single-tag trend query | ≤ 5 s |
| §528 | Dashboard refresh | 2–3 s |
| §528 | Availability over observation window | ≥ 99.5% |
| §318 | Force Bad, Uncertain, frozen, out-of-range | Each flagged, never silently valid |
| §335 | Source timestamp retention | Never replaced by receipt time |
| §319, §382, §648 | Pull the link for 3 minutes | Zero loss, chronological, no duplicates |
| §455, §649 | Kill the primary collector | Zero missing samples |
| §442 | Compression | Ratio reported, error ≤ CompDev |
| §472 | Start-up event | Auto-captured with all milestones |
| §317 | Non-intrusiveness | Source CPU and scan time unchanged, collector on vs off |
| §346, §336 | Time source loss | Behaviour defined and demonstrated |
| §303 | No control path | Code inspection plus automated test |

---

## What this demonstrator can and cannot claim

**Can:** real industrial protocols, real sensors, real quality semantics, real buffering
and recovery, real compression, real asset model, real event frames, real KPI lineage,
PI-compatible output, every behaviour measured against the tender's own numeric criteria.

**Cannot:** it does not touch a real BHEL, Yokogawa, ABB or Andritz DCS; it is not running
on AVEVA PI; it is not proven at 34,700 I/O across 13 sites; it says nothing about WAN
behaviour, cross-site time synchronisation, per-station licensing, or OT security zoning.

Say both halves. The second one is what makes the first believable.

---

## Sequence

| Week | Stages |
|---|---|
| 1 | 0, 1, 2 |
| 2 | 3, 4 — the credibility core |
| 3 | 5, 6, 7 |
| 4 | 8, 9, 10 |
| 5 | 11, 12 |
| 6 | 13, 14 |

Start Stage 1 tonight. Order the hardware in parallel; Stage 10 is the only stage that
waits on it.
