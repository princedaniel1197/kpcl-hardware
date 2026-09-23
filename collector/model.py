"""The one thing that travels through every layer of the collector."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Sample:
    """A measurement, with everything that makes it interpretable.

    `source_ts` is when the value was produced and `server_ts` is when it went
    on the wire. They are separate fields because they are separate quantities,
    and nothing in this package may set one from the other (§335). `server_ts`
    is None when no server stamped the value: a DataValue that arrived without
    a ServerTimestamp, or a value the collector measured itself.

    `value` is None when quality is Bad: an OPC UA DataValue with a Bad
    StatusCode carries no value (Part 4), measured in Stage 1. There is nothing
    to substitute, and substituting would be forbidden anyway (§318).

    `quality` is the numeric StatusCode. Never a boolean, never a string.

    `run` and `seq` are assigned by the pipeline at the moment it decides a
    sample will be archived: `seq` counts, per tag, the samples collector run
    `run` meant to write. They travel into the archive with the row, so a
    sample the collector meant to write and the archive does not hold is a hole
    anyone can find by query. They are None until then.
    """

    tag_id: int
    tag_name: str
    source_ts: dt.datetime
    server_ts: dt.datetime | None
    value: float | None
    quality: int
    seq: int | None = None
    run: int | None = None

    @property
    def quality_class(self) -> str:
        return {0: "Good", 1: "Uncertain", 2: "Bad"}.get(
            (self.quality >> 30) & 3, "Reserved")

    @property
    def is_good(self) -> bool:
        return self.quality == 0
