# Stage 14 — The FAT

| | |
|---|---|
| Stage | 14 — The FAT |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 14 |
| Deliverable | An Inspection and Test Plan, a FAT procedure with hold and witness points, and an automated runner producing a signed-off report |
| Date performed | 2026-09-22 |
| **Result** | **BUILT and exercised.** 12 automated tests pass; 5 hold and witness points are outstanding and require signatures |

## What was built

| Item | |
|---|---|
| [`fat/plan.py`](../plan.py) | The Inspection and Test Plan: 20 tests, one row each, with clause, condition, method and numeric criterion |
| [`fat/ITP.md`](../ITP.md) | The plan rendered for a reviewer — **generated**, so it cannot drift from what the runner executes |
| [`fat/procedure.md`](../procedure.md) | How the FAT is run, in what order, and why |
| [`fat/tests.py`](../tests.py) | The automated checks |
| [`fat/runner.py`](../runner.py) | Executes them, captures evidence, writes a signed-off report |
| `fat/reports/` | Generated reports |

Twelve tests are automated. Three are **hold points** and two are **witness
points**, marked as such rather than automated into a green tick that would mean
nothing.

## The result

```
| Automated tests executed | 12 |
| Passed                   | 12 |
| Failed                   |  0 |
| Hold and witness points outstanding | 5 |
```

Every figure is measured when the report is produced. Each report states the git
commit and whether the working tree was clean, because a report from a dirty
tree is evidence about nothing in particular.

## The defect this stage found, and it is the most interesting one in the build

**T-01 reported a P95 acquisition-to-historian latency of −3.278 seconds, and
passed.**

It passed because −3.278 is less than 5.

A negative latency is not a fast system; it is a broken measurement. The test
subtracted `source_ts` — stamped by the simulator on the **host** — from `now()`
evaluated inside the **database container**. Those are two different clocks.
Measured directly:

```
  database clock minus host clock, 7 samples:
    median -0.924 s   min -1.841   max -0.003
```

Up to 1.8 seconds apart, and drifting.

That is worth sitting with. This project's entire thesis is that measurement
time is not arrival time and that timestamps must be handled with care — and its
own acceptance test for timing was quietly comparing two clocks and reporting
the difference between them as system performance. It would have gone into a
tender pack as a green tick.

Fixed three ways:

1. **One clock.** "Now" is taken from the host, the same clock that stamps
   `source_ts`.
2. **A negative observation fails the test outright**, rather than being
   averaged into a percentile that looks respectable.
3. **The measured clock offset is reported as evidence** on every run, because
   anyone reading a latency figure needs to know the two clocks differ.

Re-measured: **P95 1.052 s** over 1000 observations, P50 0.822 s, against a 5 s
criterion.

This also belongs to §346 and §336. The demonstrator runs on two clocks that
disagree by up to two seconds, on one machine. Cross-site time synchronisation
across 13 stations is explicitly outside what this demonstrates, and this is a
small, concrete illustration of why it is not a footnote.

## Two smaller defects

**T-01 counted 949 observations and reported a P95.** The criterion says 1000.
It now waits for 1000 rather than reporting a percentile over fewer and calling
it the same thing.

**T-04 reported availability of 93.44% and failed.** The figure was honest and
the failure was correct — but it was misleading, because the window contained
the deliberate outage tests. The test now **names every uncovered minute**
(18:51, 18:52, 19:07, 19:08), so a reviewer can see whether the missing time was
a test or a fault. A single percentage invites the reader to assume the gap was
random, and 93% that turns out to be "we stopped the database on purpose" means
something entirely different from 93% of unexplained absence.

The procedure now states that the acceptance run needs an undisturbed window,
and one was waited for.

## Hold and witness points outstanding

| Ref | Type | Test | Needs |
|---|---|---|---|
| T-08 | hold | Archive outage and ordered recovery | Signature; result in `stage-03-collector.md` |
| T-09 | hold | Kill the primary collector | Signature; result in `stage-11-redundancy.md` |
| T-17 | hold | Backup and restore | Signature; **verify by row count** — the first version of that procedure lost 12% of the archive while reporting success |
| T-15 | witness | Adding a unit is configuration | A witness to watch it |
| T-16 | witness | Operator can read the display unaided | A colleague who has not seen it |

T-16 cannot be automated and should not be. The criterion is a human judgement,
and the people who built the display are the last people whose opinion of its
legibility is worth anything.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
