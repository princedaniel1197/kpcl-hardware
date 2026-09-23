-- Changes forced by the code review of 23 September 2026. Each block names the
-- finding it answers.

BEGIN;

-- ---------------------------------------------------------------------------
-- A4. server_ts may be absent, and absence is stored as absence.
--
-- The column was NOT NULL, so when a DataValue arrived with no ServerTimestamp
-- the collector filled it with the SOURCE timestamp. That is exactly the
-- fingerprint T-07 exists to detect -- two identical timestamps -- and the day
-- it fired the report would have said the collector substituted a receipt time
-- for a measurement time, which it had not. The only honest record of "no
-- server stamped this value" is NULL.
--
-- The collector's own health samples also land here with NULL: the collector
-- measures them, and no OPC UA server ever puts them on a wire.
-- ---------------------------------------------------------------------------
ALTER TABLE sample ALTER COLUMN server_ts DROP NOT NULL;

COMMENT ON COLUMN sample.server_ts IS
    'OPC UA ServerTimestamp as received. NULL when the source sent none, and for '
    'values the collector measures itself. Never derived from source_ts.';

-- ---------------------------------------------------------------------------
-- B1. A sequence number that can actually reveal loss.
--
-- The collector used to number samples on receipt and then check that its own
-- numbers were consecutive, which they always were. Nothing downstream ever
-- saw them. Two things replace that:
--
--   * Loss between the SOURCE and the collector is detected from the OPC UA
--     NotificationMessage.SequenceNumber, which the server assigns (Part 4).
--     That needs no schema; it is counted in the collector's health tags.
--
--   * Loss between the COLLECTOR and the ARCHIVE is made reconcilable here.
--     Each collector start is a run. Every sample the run decides to archive is
--     numbered per tag, and the number is stored with the row. A hole in a
--     run's numbers is a sample that run meant to write and that is not here.
-- ---------------------------------------------------------------------------
CREATE TABLE collector_run (
    id          bigserial   PRIMARY KEY,
    instance    text        NOT NULL,
    host        text,
    pid         integer,
    started_at  timestamptz NOT NULL DEFAULT now()
);

-- Added bare, then constrained: TimescaleDB refuses a column that carries a
-- constraint on a hypertable with its columnstore enabled, but accepts the same
-- constraint added as its own statement.
ALTER TABLE sample ADD COLUMN collector_run bigint;
ALTER TABLE sample ADD COLUMN seq bigint;
ALTER TABLE sample ADD CONSTRAINT sample_collector_run_fkey
    FOREIGN KEY (collector_run) REFERENCES collector_run(id);
ALTER TABLE sample ADD CONSTRAINT sample_run_and_seq_together
    CHECK ((collector_run IS NULL) = (seq IS NULL));

COMMENT ON COLUMN sample.seq IS
    'Per (collector_run, tag): the Nth sample that run decided to archive. A hole '
    'is a sample it meant to write that is not in the archive.';

-- When the local buffer overflows, the collector discards its oldest rows. That
-- is a real loss and it is recorded as one, here, per tag: which run, which
-- stretch of time, which sequence numbers, how many.
CREATE TABLE collector_loss (
    -- NULL run and sequence numbers only for rows a pre-migration buffer held,
    -- which were never numbered.
    collector_run    bigint      REFERENCES collector_run(id),
    tag_id           bigint      NOT NULL REFERENCES tag(id),
    first_source_ts  timestamptz NOT NULL,
    last_source_ts   timestamptz NOT NULL,
    first_seq        bigint,
    last_seq         bigint,
    samples          integer     NOT NULL CHECK (samples > 0),
    reason           text        NOT NULL,
    -- The collector's clock when it discarded them; a different quantity from
    -- the source timestamps of what was discarded.
    detected_at      timestamptz NOT NULL,
    -- One buffer never holds two rows for the same (tag, source_ts), so a
    -- discarded range is identified by its tag and first source timestamp.
    -- The collector re-sends a range it is not sure landed; this absorbs it.
    PRIMARY KEY (tag_id, first_source_ts),
    CHECK (last_seq >= first_seq),
    CHECK (last_source_ts >= first_source_ts)
);

-- Holes in a run's sequence numbers, with what, if anything, fills them.
--
-- With two collectors writing (Stage 11), a sample one run numbered may be in
-- the archive under the OTHER run's key, because the first write wins. So
-- every hole reports how many rows another run holds in the same stretch of
-- time, and how many rows the loss ledger accounts for. A hole that neither
-- explains is unexplained loss.
CREATE OR REPLACE VIEW sample_seq_gap AS
WITH ordered AS (
    SELECT s.collector_run, s.tag_id, s.seq, s.source_ts,
           lag(s.seq)       OVER w AS prev_seq,
           lag(s.source_ts) OVER w AS prev_ts
    FROM sample s
    WHERE s.seq IS NOT NULL
    WINDOW w AS (PARTITION BY s.collector_run, s.tag_id ORDER BY s.seq)
)
SELECT
    o.collector_run,
    o.tag_id,
    t.name                            AS tag_name,
    o.prev_seq,
    o.seq                             AS next_seq,
    o.seq - o.prev_seq - 1            AS missing,
    o.prev_ts                         AS after_source_ts,
    o.source_ts                       AS before_source_ts,
    (SELECT count(*) FROM sample x
      WHERE x.tag_id = o.tag_id
        AND x.source_ts > o.prev_ts AND x.source_ts < o.source_ts
        AND x.collector_run IS DISTINCT FROM o.collector_run)
                                      AS held_by_other_runs,
    (SELECT coalesce(sum(least(l.last_seq, o.seq - 1)
                         - greatest(l.first_seq, o.prev_seq + 1) + 1), 0)
       FROM collector_loss l
      WHERE l.collector_run = o.collector_run AND l.tag_id = o.tag_id
        AND l.first_seq <= o.seq - 1 AND l.last_seq >= o.prev_seq + 1)
                                      AS recorded_as_lost
FROM ordered o
JOIN tag t ON t.id = o.tag_id
WHERE o.seq - o.prev_seq > 1;

-- ---------------------------------------------------------------------------
-- B2. Compression, per tag, off by default.
--
-- Two-stage compression was a verified library that the live pipeline never
-- called. It is now called for any tag with compress = true. It defaults to
-- false so that the zero-loss measurement of Stage 3 stays reproducible: with
-- compression on, "loss" has to be redefined as "not reconstructable within
-- CompDev", and the two claims must not be confused.
-- ---------------------------------------------------------------------------
ALTER TABLE tag ADD COLUMN compress boolean NOT NULL DEFAULT false;

-- A tag cannot be compressed without the parameters compression needs. The
-- swinging door holds the open segment in memory, and max_time is what bounds
-- it (review finding C7).
ALTER TABLE tag ADD CONSTRAINT tag_compress_needs_parameters
    CHECK (NOT compress
           OR (exc_dev IS NOT NULL AND comp_dev IS NOT NULL
               AND max_time_ms IS NOT NULL));

-- ---------------------------------------------------------------------------
-- C9. No default source path.
--
-- Migration 011 removed the literal "Unit1" from the collector and put it in
-- the column default instead, where it did the same damage quietly: Unit 2's
-- tags, created from the template in Stage 5, were all filed under Unit1. A
-- tag's place in the source address space is configuration and has no
-- sensible default. NULL means the tag is not acquired from an address space
-- at all (load-test tags, the collector's own health tags).
-- ---------------------------------------------------------------------------
ALTER TABLE tag ALTER COLUMN source_path DROP DEFAULT;
ALTER TABLE tag ALTER COLUMN source_path DROP NOT NULL;

-- Correct the rows the default filled in, and record each correction.
WITH before AS (
    SELECT id, source_path FROM tag
    WHERE source_system <> 'opcua' AND source_path IS NOT NULL
), changed AS (
    UPDATE tag t SET source_path = NULL
    FROM before b WHERE t.id = b.id
    RETURNING t.id, b.source_path AS was
)
INSERT INTO audit_log (actor, entity, entity_id, field, old_value, new_value, reason)
SELECT 'migration-014', 'tag', id::text, 'source_path', was, NULL,
       'not acquired from an OPC UA address space'
FROM changed;

WITH changed AS (
    UPDATE tag SET source_path = 'Unit2'
    WHERE name LIKE 'U2\_%' AND source_path = 'Unit1'
    RETURNING id
)
INSERT INTO audit_log (actor, entity, entity_id, field, old_value, new_value, reason)
SELECT 'migration-014', 'tag', id::text, 'source_path', 'Unit1', 'Unit2',
       'Unit 2 tags were filed under Unit1 by the column default'
FROM changed;

COMMIT;
