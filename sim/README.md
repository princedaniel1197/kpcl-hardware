# sim — DCS simulator (Stage 1)

An OPC UA server modelling one 210 MW thermal unit, driven through a cold start-up.
It stands in for a real DCS. Everything downstream talks to it over OPC UA; it is
never replaced by mock data or a test fixture.

Two rules decided here, not later:
- `SourceTimestamp` is set when the value is produced; `ServerTimestamp` when it goes
  on the wire. On a changing tag they are never equal. (§335)
- Quality is a real `StatusCode`, not a boolean or a string. (§318)

Empty until Stage 1.
