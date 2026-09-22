"""Collector configuration.

Scan rates and deadbands are NOT here: they live in the tag table and are read
from it at subscribe time, because they are per-tag configuration and changing
one must not require editing Python (§317, §341).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectorConfig:
    endpoint: str = "opc.tcp://127.0.0.1:4840/orianode/crpms/"
    dsn: str = "postgresql://crpms:crpms@localhost:5432/crpms"
    buffer_path: str = "buffer/collector.sqlite"

    # Bounded, as the build plan requires. When full, the oldest buffered
    # samples are discarded -- a ring, as PI's buffering behaves -- and the loss
    # is logged and left detectable through the per-tag sequence numbers.
    buffer_max_rows: int = 500_000
    buffer_warn_fraction: float = 0.80

    # How many samples to write per round trip, live and while draining.
    batch_size: int = 500
    # How long to wait for a batch to fill before writing what we have.
    batch_linger_s: float = 0.25

    health_interval_s: float = 5.0
    # The event stream is served directly by the collector, NOT relayed through
    # the archive: an archive outage must stay visible.
    event_host: str = "127.0.0.1"
    event_port: int = 8090
    reconnect_delay_s: float = 2.0
    instance: str = "primary"

    def resolved_buffer_path(self) -> str:
        """Each instance gets its own buffer file.

        Two collectors sharing one SQLite buffer would corrupt each other's
        store-and-forward: one would drain rows the other was still holding, and
        the loss would appear as a gap nobody could explain.
        """
        if "{instance}" in self.buffer_path:
            return self.buffer_path.format(instance=self.instance)
        if self.instance == "primary":
            return self.buffer_path
        stem, _, suffix = self.buffer_path.rpartition(".")
        return f"{stem}-{self.instance}.{suffix}" if stem else \
            f"{self.buffer_path}-{self.instance}"

    @classmethod
    def from_env(cls) -> "CollectorConfig":
        return cls(
            endpoint=os.environ.get("SIM_OPCUA_ENDPOINT", cls.endpoint),
            dsn=os.environ.get("CRPMS_DSN", cls.dsn),
            buffer_path=os.environ.get("COLLECTOR_BUFFER", cls.buffer_path),
            buffer_max_rows=int(os.environ.get("COLLECTOR_BUFFER_MAX_ROWS",
                                               cls.buffer_max_rows)),
            batch_size=int(os.environ.get("COLLECTOR_BATCH", cls.batch_size)),
            instance=os.environ.get("COLLECTOR_INSTANCE", cls.instance),
            event_port=int(os.environ.get("COLLECTOR_EVENT_PORT",
                                          cls.event_port)),
        )
