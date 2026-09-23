"""Collector configuration.

Scan rates and deadbands are NOT here: they live in the tag table and are read
from it at subscribe time, because they are per-tag configuration and changing
one must not require editing Python (§317, §341). How each SOURCE server is
treated -- whether the deadband is applied at the source -- is in
config/sources.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

SOURCES_FILE = Path(__file__).parent.parent / "config" / "sources.json"
DEFAULT_BUFFER = "buffer/collector.sqlite"


def source_settings(application_uri: str, path: Path = SOURCES_FILE) -> dict:
    """The settings for one OPC UA server, identified by its ApplicationUri:
    its entry in sources.json laid over the file's defaults. A server not
    listed gets the defaults, which apply the deadband at the source.

    Matched on the server's own identity rather than on the endpoint string,
    because the same server is reachable as 127.0.0.1, localhost or a host
    name, and a setting that silently stops applying when someone types the
    address differently is not a setting."""
    document = json.loads(path.read_text())
    settings = {"source_deadband": True, **document.get("default", {}),
                "matched": None}
    for source in document.get("sources", []):
        if source.get("application_uri") == application_uri:
            settings.update({k: v for k, v in source.items()
                             if k not in ("application_uri", "endpoint")})
            settings["matched"] = source.get("name", application_uri)
            break
    return settings


@dataclass(frozen=True)
class CollectorConfig:
    endpoint: str = "opc.tcp://127.0.0.1:4840/orianode/crpms/"
    dsn: str = "postgresql://crpms:crpms@localhost:5432/crpms"
    buffer_path: str = "buffer/collector.sqlite"   # DEFAULT_BUFFER

    # Bounded, as the build plan requires. When full, the oldest buffered
    # samples are discarded -- a ring, as PI's buffering behaves. The loss is
    # counted in the BUFFER_LOST health tag and every discarded range is
    # recorded, with its sequence numbers, in the archive's collector_loss.
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
        the loss would appear as a gap nobody could explain. So the DEFAULT path
        gets the instance name added for any instance but the primary. A path
        given explicitly is used as given (or with `{instance}` filled in) --
        appending to it as well produced names nobody asked for, which the
        Stage 11 test's clean-up then failed to find.
        """
        if "{instance}" in self.buffer_path:
            return self.buffer_path.format(instance=self.instance)
        if self.instance == "primary" or self.buffer_path != DEFAULT_BUFFER:
            return self.buffer_path
        stem, _, suffix = self.buffer_path.rpartition(".")
        return f"{stem}-{self.instance}.{suffix}"

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
