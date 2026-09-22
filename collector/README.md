# collector — acquisition (Stages 3, 4, 9, 11)

Subscribes to the Stage 1 OPC UA server with per-tag deadbands, forwards to the
archive, buffers locally when the forward fails, and replays in ascending
source-timestamp order on restore.

**The session is read-only.** No write path exists in this package. A test asserts
its absence. (§303, §315)

Empty until Stage 3.
