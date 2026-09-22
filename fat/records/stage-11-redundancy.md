# Stage 11 — Redundancy

| | |
|---|---|
| Stage | 11 — Redundancy |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 11 |
| Acceptance criterion | Kill the primary collector during a start-up event. The event frame is still complete. Zero missing samples across the transition (§455, §649). |
| Date performed | 2026-09-22 |
| Method | `python -m collector.redundancy_demo --startup-seconds 180` |
| **Result** | **PASS** — 12 of 12 checks |

## Two collectors, one archive, no arbitration

```
  instance         pid  alive  leader   samples  link
  primary        83596  True   True        293  True
  secondary      83600  True   False       292  True
```

Both write freely. There is nothing to arbitrate, because the
`(tag_id, source_ts)` primary key makes a duplicate impossible — so redundancy
here needs no consensus protocol, no fencing and no split-brain handling. That
is a property of the schema, decided at Stage 2, not of any code written here.

**The leader indication is for reporting only.** It answers "which collector
should the dashboard attribute this to", not "which one is allowed to write".
Nothing consults it before writing, and a wrong answer costs a label rather than
data. It is derived in a view — the longest-running instance still reporting —
rather than stored, because a stored flag can be stale in a way a query cannot.

## Killing the primary mid-start-up

```
  killing the PRIMARY at 18:59:53 (unit in PRESSURE_RAISE)
     primary is gone

  instance         pid  alive  leader   samples
  primary        83596  False  False      1738
  secondary      83600  True   True       5394
```

Leadership moved to the survivor without anything being told to move it.

## Zero missing samples across the transition

```
  threshold is each tag's own max-time heartbeat plus 5 s

  U1_MS_TEMP       threshold  65.0s   gaps: 0   worst: none
  U1_TURB_SPEED    threshold  65.0s   gaps: 0   worst: none
  U1_MW            threshold  15.0s   gaps: 0   worst: none
  U1_DRUM_PRESS    threshold  65.0s   gaps: 0   worst: none

  rows in the window: 4,836   distinct keys: 4,836
```

Zero duplicates despite two independent writers, and no coverage gap on any tag
across the kill.

## The event frame is still complete

```
  start 19:03:36  end 19:06:24  duration 2m 48.0s
    boiler_lightup             2.0s
    steam_admission        1m 02.0s  <-- primary killed here
    turbine_rolling        1m 27.0s  <-- primary killed here
    rated_speed            1m 57.5s
    synchronisation        2m 03.0s
    full_load              2m 48.0s
```

The primary died between steam admission and turbine rolling. All six
milestones are present, the frame spans the kill, and nothing in the frame
records that anything happened to the monitoring system — which is the point.

## A defect in the test, not the system

The first run reported gaps of 62.5 s on `U1_TURB_SPEED` and 12.5 s on `U1_MW`
against a flat 6 s threshold, and called them failures. They were not. Both tags
were sitting still — speed at standstill, generation before synchronisation —
and a tag that reports on change produces samples only at its **max-time
heartbeat** when nothing is moving. 62.5 s was exactly the 60 s heartbeat plus
jitter.

A fixed threshold cannot express "this tag should have reported by now",
because the answer differs per tag and is already configuration. The check now
uses each tag's own `max_time_ms`. The original version would have failed every
run forever while the system was working correctly, which is the kind of test
that gets deleted rather than fixed.

Two smaller ones, both mine: an earlier patch replaced the wrong
`return cur.fetchall()` and broke `leaders()`; and each instance needs its own
SQLite buffer file, or two collectors sharing one would drain rows the other was
still holding — a loss that would appear as a gap nobody could explain.

## Health tags carry the instance

`COLLECTOR_PRIMARY_LINK_STATE`, `COLLECTOR_SECONDARY_BUFFER_DEPTH`, and so on —
sixteen tags, eight measurements per instance. Without the instance in the name
the two collectors would overwrite each other's account of themselves, and the
health trend would be the average of two different stories.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
