# archive — schema and migrations (Stages 2, 9)

Numbered SQL migrations for TimescaleDB, in `migrations/`. Mounted read-only into
the database container at `/migrations`; migrations are applied deliberately, never
auto-run on container start.

The primary key on `sample` is `(tag_id, source_ts)`. Idempotent replay is a
property of the schema, not of the code that writes to it. (§382, §648)

Empty until Stage 2.
