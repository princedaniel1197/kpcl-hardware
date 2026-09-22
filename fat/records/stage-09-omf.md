# Stage 9 — OMF emitter

| | |
|---|---|
| Stage | 9 — OMF emitter |
| Build plan | `CRPMS_Demonstrator_Build_Plan.md`, Stage 9 |
| Acceptance criterion | Messages validate against the OMF schema. Changing one config value switches the destination. |
| Date performed | 2026-09-22 |
| Specification | AVEVA OMF 1.2, github.com/AVEVA/OMF-Docs (Apache-2.0), fetched and read during implementation |
| **Result** | **PASS** — 9 of 9 checks |

## Validation

```
  1. THE THREE MESSAGE KINDS
     type       2 object(s)  VALIDATES
     container  14 object(s)  VALIDATES
     data       1 object(s)  VALIDATES

     index property  : ['SourceTime']
     quality property: ['Quality']
```

**A statement about what "validates" means here.** The AVEVA repository
publishes the specification as documentation, not as a JSON Schema file — the
repository tree contains no schema document. `collector/omf.validate` therefore
implements the documented rules directly: required keywords, the `x.x.x.x`
version format, reserved `__` type ids, the Supported Formats table, at least
one `isindex` property, at most one `isname`. That is a structural validator
written from the specification rather than the specification's own schema, and
saying otherwise would overstate the evidence.

Rules verified by test, each with a message that breaks it: missing index,
reserved id, unsupported format, malformed version, two `isname` properties,
container without `typeid`, data without container or type, empty body.

## Two things the specification made possible

**SourceTime is the index.** A dynamic OMF type must designate one property as
`isindex`, and that is what the historian orders and de-duplicates on. Making it
the source timestamp rather than the server timestamp carries §335 all the way
into PI: what is archived is indexed by when the value was produced. ServerTime
rides alongside as an ordinary property, so the transit stays visible and stays
separate.

**Quality is first-class in OMF.** The specification provides `isquality`:
"one or more Type Properties may optionally be designated as quality... these
properties would then determine the overall quality of each data value." The
numeric OPC UA StatusCode therefore travels as a designated quality property
rather than being smuggled through as an ordinary number.

## And one it made dangerous

From the Data message specification: *"If a property is defined on the Type
definition, and that property is not included in the values array, then a
default value for that property will be assumed."* The default for `number` is
**0**.

So expressing "this sample has no value" by omitting `Value` — the obvious way —
would silently store a **zero**. That is exactly the substitution §318 forbids,
arriving through the wire format rather than through our code, and nothing in
the data afterwards would reveal it.

The emitter therefore always sends `Value`, explicitly `null` when the sample
carries none, and the receiver **refuses** a message that omits it:

```
  3. A VALUE OMITTED IS REFUSED
     refused: OMF endpoint returned 400: U1_AUX_POWER: Value was omitted.
              OMF would default it to 0; send an explicit null

     stored: [(123.45, 0), (None, 2156593152)]
```

The Bad sample stored NULL with its StatusCode intact, not zero.

## Changing one config value switches the destination

```
  4. CHANGING ONE CONFIG VALUE (port 8082 -> 8083)
     receiver A: 1 data messages (unchanged)
     receiver B: 1 data messages
```

Only the URL differed; the producer token and every message were identical.
Pointing the collector at a real PI Web API OMF endpoint is `CRPMS_OMF_URL`
plus `CRPMS_OMF_USER` / `CRPMS_OMF_PASSWORD`, and no code change. Basic
authentication is built from those, alongside the producer token.

## The collector's only output format is OMF

Measured with `CRPMS_OMF_URL` set, over 30 s against the live simulator:

```
  samples written via OMF: 818
  receiver: {'type': 2, 'container': 36, 'data': 811, 'values': 821, 'rejected': 0}
```

Everything upstream is unchanged — the buffer, the ordered drain, the
`(tag_id, source_ts)` idempotency — because the sink is the only thing that
differs. The direct-SQL sink remains the default, so Stage 3's zero-loss result
continues to describe what was actually measured.

## Two defects found by running it

| Defect | Evidence | Fix |
|---|---|---|
| Containers were created only for source tags, so the collector's own health stream was rejected by the endpoint | 60 rejected messages in 30 s, all health. Plant data kept flowing; only the monitoring vanished | Containers are created for every tag the collector will send, health included |
| A rejected data message cleared the registration flag, so the next write re-registered everything | 120 type messages and 1680 container messages in 30 s — a re-registration storm caused by one bad batch | A rejected data message no longer implies the types and containers are gone |

Both were invisible in the happy path and only appeared in the receiver's
counters. They are the reason the receiver has counters.

## Honest limits

This receiver is not PI and does not pretend to be. What the stage demonstrates
is that the collector speaks a real, published, PI-compatible wire format and
that the destination is configuration. Whether an actual AVEVA PI Web API
endpoint accepts these exact messages has **not** been tested, because this
project has no PI licence — that test needs a real endpoint and is the obvious
first thing to do if one becomes available.

## Tests

`pytest collector/test_omf.py -q` → **22 passed**.

## Signature

| Role | Name | Date |
|---|---|---|
| Performed by | | 2026-09-22 |
| Witnessed by | | |
