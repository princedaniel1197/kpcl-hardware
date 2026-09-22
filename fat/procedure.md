# Factory Acceptance Test — procedure

Orianode Technologies · CRPMS Demonstrator · for KPCL

This is how the FAT is run. The Inspection and Test Plan — one row per test with
its clause, condition, method and numeric criterion — is generated from
`fat/plan.py` into every report, so the plan and the report cannot drift apart.

## Before starting

| | |
|---|---|
| Prerequisites | Python 3.11+, Docker, Node 20+ — `make doctor` reports all three |
| Database | `make up`, then `make migrate` |
| Configuration | `make seed-tags seed-assets seed-quality seed-kpis seed-alerts` |
| Source | `make sim` — the OPC UA simulator |
| Acquisition | `make collector` |
| API and UI | `make api`, `make ui` |

**Record the commit.** Every report states the git commit it was produced from
and whether the working tree was clean. A report from a dirty tree is evidence
about nothing in particular.

## Running the automated tests

```bash
make fat                       # every automated test
.venv/bin/python -m fat.runner --only T-01,T-07
.venv/bin/python -m fat.runner --skip T-12
```

The report lands in `fat/reports/FAT-<UTC timestamp>.md`. It contains one row
per test, the measured result, the evidence behind it, and signature blocks.

**Every figure in a report is measured when the report is produced.** Nothing is
carried over from a previous run.

## Order matters

Two tests are destructive and must not overlap anything else:

- **T-12 (non-intrusiveness)** detaches the collector for 20 seconds.
- **T-08 (archive outage)** stops the database for three minutes.
- **T-09 (kill the primary)** kills a collector mid start-up.

Run the long-form ones first, then let the system run **undisturbed for at least
an hour** before the acceptance run, because **T-04 measures availability over a
window and a deliberate outage counts against it.** Every uncovered minute is
named in the report, so a reviewer can see whether the missing time was a test
or a fault — but an acceptance figure should come from a window with no
destructive test in it.

## Hold points

Work stops at a hold point until it is signed off.

| Ref | Test | Why it is a hold point |
|---|---|---|
| T-08 | Archive outage and ordered recovery | The central claim of the system. Nothing else matters if this fails. |
| T-09 | Kill the primary collector | Redundancy is either demonstrated or it is not. |
| T-17 | Backup and restore | **Verify by row count.** The first version of this procedure reported success while losing 12% of the archive. |

## Witness points

Must be observed by a person and signed.

| Ref | Test | What the witness does |
|---|---|---|
| T-15 | Adding a unit is configuration | Watches one object added to `config/asset_model.json` produce a complete unit with its tags, and confirms no Python was edited |
| T-16 | Operator can read the display unaided | Watches an outage and recovery on the visualisation and describes what happened. **If they cannot, the display is wrong, not the viewer.** |

T-16 cannot be automated and should not be. The criterion is a human judgement,
and the people who built it are the last people whose opinion of its legibility
is worth anything.

## Reading a failure

A failure in this report is a failure. It is listed in full, with its measured
value, and is not summarised away in the totals at the top.

Two kinds are worth telling apart:

- **The system did not meet the criterion.** Fix the system.
- **The test measured the wrong thing.** Fix the test — and record that it was
  wrong, because a test that was quietly corrected until it passed is not
  evidence of anything.

Both have happened during this build and both are recorded in `fat/records/`.

## After the run

1. Attach the generated report to the FAT pack.
2. Complete the hold and witness signatures.
3. File the per-stage records from `fat/records/` alongside it. They carry the
   defects found during construction, which is context a single pass/fail table
   cannot give.
