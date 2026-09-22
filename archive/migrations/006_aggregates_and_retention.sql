-- migrate:no-transaction
--
-- Continuous aggregate and retention policy. (§528 for query performance,
-- and the ten-year retention the build plan asks for.)
--
-- TimescaleDB refuses to create a continuous aggregate inside a transaction
-- block, hence the marker above; the runner applies this file with autocommit.

-- One-minute averages.
--
-- The aggregate deliberately does NOT average everything it finds. A mean
-- computed over a window that was half Bad is not the same quantity as a mean
-- over a clean window, and a chart that cannot tell the difference is the kind
-- of smoothing this project exists to avoid. So: statistics are over Good
-- samples only, and the counts that qualify them are carried alongside. A
-- consumer can always ask "how much of this minute was actually good?".
CREATE MATERIALIZED VIEW sample_1min
WITH (timescaledb.continuous) AS
SELECT
    tag_id,
    time_bucket(INTERVAL '1 minute', source_ts)          AS bucket,
    avg(value) FILTER (WHERE quality = 0)                AS avg_good,
    min(value) FILTER (WHERE quality = 0)                AS min_good,
    max(value) FILTER (WHERE quality = 0)                AS max_good,
    count(*)                                             AS sample_count,
    count(*) FILTER (WHERE quality = 0)                  AS good_count,
    count(*) FILTER (WHERE ((quality >> 30) & 3) = 1)    AS uncertain_count,
    count(*) FILTER (WHERE ((quality >> 30) & 3) = 2)    AS bad_count
FROM sample
GROUP BY tag_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('sample_1min',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');

-- Ten years, as the build plan specifies.
SELECT add_retention_policy('sample', INTERVAL '10 years');

-- Compression on chunks older than a week. Not the two-stage process
-- compression of Stage 4 -- that is about which samples are worth archiving at
-- all. This is TimescaleDB storing the ones we kept more cheaply.
ALTER TABLE sample SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tag_id',
    timescaledb.compress_orderby   = 'source_ts DESC'
);
SELECT add_compression_policy('sample', INTERVAL '7 days');
