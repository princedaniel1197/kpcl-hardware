# Stage 8 — Event frames

| | |
|---|---|
| Stage | 8 — Event frames |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 8 |
| Acceptance criterion | Run a simulated start-up. The frame is captured automatically with all milestones. Run a second, slower one. The comparison reports the delta per milestone. |
| Date performed | 2026-09-22 |
| Method | `python -m engine.events_demo --fast 120 --slow 240` — drives two cold start-ups on the live simulator, then detects and compares over the real archived history |
| **Result** | **PASS** — 10 of 10 checks |

## Two start-ups, captured automatically

```
  FRAME 1 (fast run)          duration 1m 40.5s
    milestone                offset       value  quality
    boiler_lightup             2.5s        1.00  0
    steam_admission           37.0s      121.80  0
    turbine_rolling           52.0s      484.67  0
    rated_speed            1m 10.0s     3001.14  0
    synchronisation        1m 13.5s        1.00  0
    full_load              1m 40.5s      209.96  0

  FRAME 2 (slower run)        duration 3m 21.5s
    boiler_lightup             2.5s        1.00  0
    steam_admission        1m 14.0s      110.21  0
    turbine_rolling        1m 44.0s      209.87  0
    rated_speed            2m 20.5s     3002.16  0
    synchronisation        2m 27.5s        1.00  0
    full_load              3m 21.5s      208.45  0
```

All six milestones present in both, in order, each carrying the quality of the
sample that satisfied it.

## Comparison (§475)

Against the stored reference curve, and against the previous best:

```
    boiler_lightup reached 0.0s earlier (2.5s against 2.5s)
    steam_admission reached 37.0s later (1m 14.0s against 37.0s)
    turbine_rolling reached 52.0s later (1m 44.0s against 52.0s)
    rated_speed reached 1m 10.5s later (2m 20.5s against 1m 10.0s)
    synchronisation reached 1m 14.0s later (2m 27.5s against 1m 13.5s)
    full_load reached 1m 41.0s later (3m 21.5s against 1m 40.5s)
```

This is the wording the clause asks for — "synchronisation reached 1 minute 14
seconds later" — and the deltas are measured from each event's own start, since
two start-ups happen at different wall-clock times and only the offsets are
comparable.

The reference is a real captured event marked as the benchmark, not a table of
idealised numbers, so what a start-up is measured against is a start-up that
actually happened.

## Two design decisions

**The milestone timestamp is when the condition became true, not when the
debounce expired.** Those differ by the debounce, and the debounce is an
artefact of how carefully we are watching rather than a fact about the plant.
Reporting the later one would make every start-up look slower than it was —
consistently, invisibly, and in exactly the measurement §475 exists to compare.

**Summaries are computed over Good samples only**, with the counts carried
alongside, for the same reason the Stage 2 continuous aggregate does it.

## Three defects found, all in the state machine

**1. The debounce could never expire on a digital.** Feeding a debouncer only
when its own tag reports cannot work for a change-of-state tag: digitals are
written on transition and then fall silent (§440), so a "true for 2 s" debounce
on a digital is never confirmed — the tag goes quiet the instant the condition
becomes true. Measured: `LightUp` produced 4 samples across two start-ups and
**no frame was ever opened**.

Fixed by driving every debouncer from the **stream clock**, against the last
known value of its attribute. Time passes whether or not a particular instrument
says so.

**2. The start trigger was level-triggered, so it fired on a unit already
running.** Detection over a steady-state window produced **six spurious frames,
each 2.5 s long with every milestone at the same offset**. A start-up begins when
light-up *happens*, not when somebody notices it is already true.

Fixed by making the start condition edge-triggered — it must observe the
condition false before it can fire. Milestones inside a frame stay
level-triggered, because a milestone is "the first moment this held during this
event", and the start condition is frequently also the first milestone.

**3. The demo's detection window was too narrow**, which masked the real
behaviour and made the first two failures look worse than they were. The window
is now derived from when the demo began and printed with the frames found.

## Tests

`pytest engine/test_events.py -q` → **16 passed**, covering spike rejection,
debounce timing, Bad-sample rejection, edge versus level triggering, and the
comparison arithmetic including the §475 wording.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
