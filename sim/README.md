# sim — DCS simulator (Stage 1)

An OPC UA server modelling one 210 MW thermal unit, driven through a cold
start-up. It stands in for a BHEL maxDNA or Yokogawa CENTUM DCS. Everything
downstream in this project talks to it over real OPC UA; it is never replaced by
mock data or a test fixture, because a stack that has only ever been fed
convenient data has not been demonstrated at all.

```bash
make sim                                  # 90-second start-up, control API on :8081
SIM_STARTUP_SECONDS=10800 make sim        # three-hour start-up
```

| | |
|---|---|
| OPC UA endpoint | `opc.tcp://0.0.0.0:4840/orianode/crpms/` |
| Namespace | `urn:orianode:crpms:sim` |
| Control API | `http://127.0.0.1:8081/docs` |
| Tags | 11 analogues, 3 digitals, under `Objects/Unit1` |
| Scan interval | 500 ms, configurable |

## What it models, and what it does not

`plant.py` shapes signals. It keyframes each tag per phase, interpolates
smoothly and adds noise. **It is not a physical model.** Nothing in it is
derived from a steam table, an energy balance or a turbine characteristic. A
value being plausible is not the same as a value being computed. Plant physics
enters this project at Stage 7 through published equations, or it does not enter
at all (§486).

Phases, in order: `OFFLINE → LIGHT_UP → PRESSURE_RAISE → TURBINE_ROLLING →
SYNCHRONISATION → LOADING → STEADY`. The whole sequence scales with
`--startup-seconds`; the values reached and their order do not.

One invariant is enforced rather than shaped: **`U1_MW` is exactly zero whenever
`U1_BREAKER_CLOSED` is false.** The digital and the analogue can never disagree,
and noise is never added to a quantity that is genuinely zero — a shaft at
standstill reads 0 rpm, not 0.2.

## Timestamps

`SourceTimestamp` is the instant the simulator computed the value.
`ServerTimestamp` is the instant it entered the address space. The gap between
them is `--field-latency-ms` (default 40 ms), which models field-to-server
transit. Setting it to zero does not make the two equal; it only makes the gap
uninteresting.

Measured over ten consecutive reads of `U1_MW` under load: the two timestamps
differed on every sample, by 13–20 ms, while the value climbed 0.1 → 136 MW.

## What asyncua 2.0.1 actually does — measured, not assumed

The build plan was written against asyncua 1.x. These were verified against
2.0.1 on 2026-09-22 and are the reason the code looks the way it does.

| Behaviour | Finding |
|---|---|
| `DataValue.StatusCode_` | Renamed to `StatusCode` in 2.x |
| `SourceTimestamp` we set | **Preserved exactly** |
| `ServerTimestamp` we set | **Discarded** — a year-2000 sentinel came back as "now". The server owns it and stamps it at write |
| `ServerTimestamp` on repeated reads | Stable — it is publish time, not read time |
| Reading a non-Good value | **`read_data_value()` raises `UaStatusCodeError` by default.** Pass `raise_on_bad_status=False` |
| A Bad value's `Value` | **`None`**, VariantType Null — OPC UA Part 4 says a Bad status means the value shall be null |
| An Uncertain value's `Value` | **Retained** |

The last two matter beyond this stage. Bad and Uncertain are genuinely different
things — one carries a number, one does not — which is precisely the distinction
§318 and CLAUDE.md rule 2 demand. And a Bad sample has no value to substitute,
so `sample.value` must be nullable in the Stage 2 schema, and Stage 3's
collector must not assume a number is present.

## Digitals are state, not samples

A digital is written when it changes and at no other time (§440). A subscriber
sees transitions, not a stream of identical values, and a digital that has not
changed keeps the `SourceTimestamp` of the transition that set it. Initial
states are published once at start-up so nothing is left undefined.

## Control API

Forces tag quality so a fault can be induced during a demonstration without
touching the simulator's logic or restarting anything.

```bash
curl -X POST localhost:8081/quality/U1_COAL_FLOW \
     -H 'Content-Type: application/json' -d '{"quality":"BadDeviceFailure"}'
curl -X DELETE localhost:8081/quality/U1_COAL_FLOW      # restore
curl localhost:8081/status
```

Forceable: `Good`, `BadDeviceFailure`, `UncertainSensorNotAccurate`.

**It forces quality only.** There is no endpoint that writes a value and there
must never be one — that would be a control path into the simulated plant, and
this project does not have one anywhere (§303, §315). A test asserts no route
path contains "value", and OPC UA clients are refused writes outright: every
variable is created non-writable, and a client attempting `write_value` gets a
`UaStatusCodeError`.

## Tests

```bash
.venv/bin/python -m pytest sim/ -q      # 13 tests
```

They drive a real OPC UA server over a real socket and call the control API over
real HTTP — not an in-process test client, because the build plan's test says
the fault is induced *through the control API*, and a shim would not exercise
what the demonstration uses.

To see the stage test's output:

```bash
.venv/bin/python -m pytest sim/test_stage1.py::test_ten_reads_of_u1_mw_have_distinct_timestamps -q -s
```
