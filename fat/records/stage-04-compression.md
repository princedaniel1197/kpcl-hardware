# Stage 4 — Compression

| | |
|---|---|
| Stage | 4 — Compression |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 4 |
| Acceptance criterion | On a slow tag like U1_MS_TEMP, compression ratio exceeds 10:1 and maximum reconstruction error stays within comp_dev. On a noisy tag like U1_BEARING_VIB the ratio is low — which is correct, and worth being able to explain. |
| Date performed | 2026-09-22 |
| Method | `python -m collector.compression_report --seconds 180` — subscribes to the live simulator at full scan rate with no deadband, so what is compressed is everything the DCS produced, then applies each tag's configured ExcDev and CompDev. |
| **Result** | **PASS** |

## Measured against live simulator data

180 s spanning a cold start-up through to steady operation.

```
tag                     raw   arch    ratio  CompDev  worst err   verdict
U1_AUX_POWER            361     16    22.6:1   0.240     0.2383   OK
U1_BEARING_VIB          307    245     1.3:1   0.050     0.0482   OK
U1_COAL_FLOW            352     16    22.0:1   1.800     1.7740   OK
U1_CONDENSER_VAC        351     17    20.6:1   3.300     3.2753   OK
U1_DRUM_PRESS           361     23    15.7:1   0.900     0.8631   OK
U1_FEEDWATER_FLOW       351     17    20.6:1   7.500     7.4747   OK
U1_GEN_STATOR_TEMP      361     18    20.1:1   0.750     0.7478   OK
U1_MS_PRESS             361     17    21.2:1   0.750     0.7405   OK
U1_MS_TEMP              361     19    19.0:1   2.400     2.3879   OK
U1_MW                   168     14    12.0:1   1.000     0.9957   OK
U1_TURB_SPEED           226     27     8.4:1   3.600     3.5706   OK

  tags within CompDev : 11/11
```

| Criterion | Measured | Result |
|---|---|---|
| U1_MS_TEMP ratio exceeds 10:1 | **19.0:1** | **PASS** |
| U1_MS_TEMP reconstruction error within CompDev | **2.3879 ≤ 2.400** | **PASS** |
| U1_BEARING_VIB ratio is low | **1.3:1** | **PASS** |
| All tags within CompDev | **11 of 11** | **PASS** |

## Why the vibration ratio is low, and why that is right

`U1_BEARING_VIB` has a CompDev of 0.05 mm/s against a noise sigma of 0.18 mm/s —
deliberately **below** the noise floor. Every other tag has CompDev at roughly
three sigma, above the noise, so compression discards noise rather than signal.
Vibration is the exception on purpose: excursions are the reason the tag exists,
so detail is kept and a 1.3:1 ratio accepted.

That is the explanation the build plan asks for: the ratio is a property of the
deadband relative to the signal's own noise, not a property of the algorithm. A
high ratio on this tag would mean real vibration detail had been thrown away.
The rationale is recorded per tag in `config/unit1_tags.json`, because
compression parameters are configuration (§442).

## Unit tests

`pytest collector/test_compression.py -q` → **39 passed**, over ramp, sine, step,
flat and noise at CompDev from 0.05 to 5.0.

The four exceptions each have a test: quality change on a perfectly flat tag,
every quality transition, a value that is not a number, a timestamp that does
not advance, and max_time on a dead-flat tag. Plus: nothing archived twice,
order preserved, first and last points always kept, a flat tag compressing over
100:1, and noise compressing below 3:1.

## Three defects found, all in the bound

The build plan asks that reconstruction error never exceed CompDev. Getting
there took three fixes, and the first is a property of the published algorithm
rather than a coding slip.

**1. The textbook swinging door does not hold this bound.** The cone test proves
that *a* line within CompDev of every point exists; it does not prove the line
actually drawn — anchor to archived point — is that line. Measured on a sine
with the cone test alone, realised error reached **1.6 to 1.9 times CompDev**.
The classical algorithm bounds error at roughly 2·CompDev.

Fixed by keeping the cone as the trigger for *when* to archive, then verifying
that interpolating to the chosen point keeps every discarded point within
CompDev, and walking back to an earlier point when it does not.

**2. Points discarded by exception reporting were never checked.** Stage one
drops values within ExcDev, so the door never sees them and nothing verified
them against the final line. The end-to-end bound was therefore ExcDev+CompDev
at best, and not even that reliably: measured on real `U1_DRUM_PRESS` data, the
worst error was **0.6247 against a 0.600 bound**.

Fixed by having the door witness every raw sample even when stage one discards
it, and verify the bound against those witnesses.

**3. Forced archives bypassed the verification.** The max_time path emitted the
pending point without checking the span behind it. Measured on real
`U1_CONDENSER_VAC` data: **3.7645 against a CompDev of 3.300**.

Fixed by draining the open segment through the same verified path used by
flush, inserting intermediate points until the bound holds.

With all three fixed, the two-stage pipeline guarantees **CompDev end to end
against the raw series** — stronger than the ExcDev+CompDev that two-stage
compression conventionally claims. Worst case across all synthetic shapes:
0.9989 × CompDev.

## A note for the FAT report

The first defect is worth stating to a reviewer, because it is the difference
between quoting a specification and meeting it. An implementation that follows
US4669097A faithfully and then advertises CompDev accuracy is out by up to a
factor of two, and nothing in the data would reveal it. The verification step
costs holding the current segment in memory, which max_time bounds.

## Scope

Compression deliberately discards samples, which is the opposite of the
guarantee Stage 3 tests. The two are reconciled by *what* is discarded: only
values reconstructible from their neighbours to within CompDev, which the
reconstruction test proves. This module is not wired into the live pipeline by
default; Stage 3's zero-loss result was measured with it off.

## Addendum — 23 September 2026

**"Not wired into the live pipeline by default" was a disclosure in a docstring
while the README table said "passed".** Compression now runs in the live
pipeline for any tag with `compress = true`; every tag is `false` as shipped, so
the archive stays uncompressed and Stage 3's zero-loss claim stays a claim
about every sample. max_time is now required with CompDev, in the code and in
the schema.

Measured through the collector's own Pipeline against the simulator
(`make compression-report`, 120 s at steady load, 23 September):

```
U1_MS_TEMP      241 raw ->  7 archived   34.4:1   worst 2.3851 <= 2.400
U1_BEARING_VIB  241 raw -> 206 archived   1.2:1   worst 0.0487 <= 0.050
tags within CompDev: 15/15;  overall 3,112 raw -> 461 archived, 6.8:1
```

The claim "textbook swinging door is out by 1.6–1.9 × CompDev" is now a test:
`test_compression.py` carries an independent textbook implementation and shows
it at 1.59–1.68 × CompDev on its sine at four CompDev values, against ≤ 1.0 for
this implementation.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
