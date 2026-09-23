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

## Correction and addendum — 23 September 2026

**The explanation of the −3.278 s latency above is wrong.** The container's
clock does not lag the host's. `now()` in PostgreSQL is the start time of the
current *transaction*, and the runner held one connection with one transaction
open for the whole run, so "now" was minutes stale — and the "offset of up to
1.8 s, drifting" was the age of that transaction. Measured with
`clock_timestamp()` on 23 September, the two clocks agree to about a
millisecond (median −0.64 ms over 40 samples). The fix made at the time — host
clock on both sides — was right; the reason given was not, and the paragraph
drawing a lesson about cross-site time synchronisation from it drew it from a
measurement error. The runner now uses autocommit and `clock_timestamp()`.

The review of 23 September (`review-2026-09-23.md`) found that three automated
tests could not fail — T-04, T-12 and the gap count behind T-08 — and that
T-18–T-20 were not run by the FAT at all. Every automated test now states what
would make it fail; the criteria of T-04, T-05, T-10, T-12 and T-13 changed;
T-18–T-20 run against the live system; and T-12 is shown failing an aggressive
polling client (`make intrusion-demo`: 3.37 % of a core per tag against a 0.5 %
budget). The report `FAT-20260922T194953Z.md` was produced from a dirty tree;
the runner now refuses one.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |

### Run of 23 September, 04:27 UTC — 14 of 15, T-05 failed

`FAT-20260923T042721Z.md`, from a clean tree at `59d66e8`. It started an hour
early, by mistake, and is kept because it found a real defect: **T-05 failed on
its frozen-value check.** The Stage 6 demonstration waited a fixed 10 s + 65 s
after restarting the start-up before checking that U1_MW, pinned at 0 while the
breaker is open, was flagged frozen. With the simulator's 90 s start-up the
breaker closes 58 s in, so U1_MW was already ramping; the rule itself flagged
frozen three times during the same run. It passed on 22 September only because
the timings then happened to fit. The demonstration now times the check from the
simulator's own clock and start-up length (52 s after the restart here), and
says so rather than failing if a start-up is too short to hold the window.
Re-run alone: 15 of 15.

The same run measured T-12 at **0.027 % of one core per subscribed tag** (budget
0.5 %) and T-04 at 8,176 of 8,176 samples — over a 6-minute window, which is why
it is not the acceptance report.
