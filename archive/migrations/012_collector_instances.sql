-- Collector instances and leader indication. (§455, §649, Stage 11)
--
-- Two collectors run against the same source and the same archive, and BOTH
-- write freely. De-duplication is already guaranteed by the (tag_id, source_ts)
-- primary key, so there is nothing to arbitrate: the schema makes a second copy
-- of a sample impossible, which is why redundancy here needs no consensus
-- protocol, no split-brain handling and no fencing.
--
-- The leader indication below is FOR REPORTING ONLY. It answers "which
-- collector should the dashboard attribute this to", not "which collector is
-- allowed to write". Nothing consults it before writing, and a wrong answer
-- costs a label, not data. That distinction is the whole reason this is a
-- simple table rather than a distributed lock.

BEGIN;

CREATE TABLE collector_instance (
    instance    text        PRIMARY KEY,
    host        text,
    pid         integer,
    started_at  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL,
    samples     bigint      NOT NULL DEFAULT 0,
    link_up     boolean     NOT NULL DEFAULT false,
    buffer_depth integer    NOT NULL DEFAULT 0
);

-- The leader is the longest-running instance still reporting. Derived, not
-- stored: a stored flag can be stale in a way a query cannot.
CREATE OR REPLACE VIEW collector_leader AS
SELECT
    instance, host, pid, started_at, last_seen, samples, link_up, buffer_depth,
    now() - last_seen AS age,
    (last_seen > now() - INTERVAL '30 seconds') AS alive,
    instance = (
        SELECT instance FROM collector_instance
        WHERE last_seen > now() - INTERVAL '30 seconds'
        ORDER BY started_at, instance LIMIT 1
    ) AS is_leader
FROM collector_instance
ORDER BY started_at;

COMMIT;
