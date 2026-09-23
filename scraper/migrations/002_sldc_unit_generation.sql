-- Karnataka SLDC recorder — per-unit generation.
--
-- The page publishes every unit's MW alongside each station total, every
-- minute. The first version of the recorder read only the totals and threw the
-- units away. The tender is a per-unit system (58 units), so the per-unit figure
-- is the part of this feed worth having; it is recorded from here on. What was
-- discarded before this migration is not recoverable.
--
-- Same conventions as 001: the page's timestamp is source_ts, the fetch time is
-- server_ts, neither is derived from the other; quality is the numeric OPC UA
-- StatusCode; an unreadable value is NULL with a Bad code and a reason, never 0.

BEGIN;

CREATE TABLE IF NOT EXISTS sldc_unit_generation (
    station         text                     NOT NULL,
    unit            smallint                 NOT NULL,  -- UNIT n column on the page
    source_ts       timestamptz              NOT NULL,  -- the page's timestamp
    server_ts       timestamptz              NOT NULL,  -- when we fetched it
    generation_mw   double precision,                   -- NULL when not Good
    quality         bigint                   NOT NULL,  -- OPC UA StatusCode
    reason          text,                               -- why, when not Good
    PRIMARY KEY (station, unit, source_ts),
    CHECK (unit >= 1),
    -- A Bad reading carries no number. The schema, not the writer, enforces it.
    CHECK (quality = 0 OR generation_mw IS NULL)
);

SELECT create_hypertable('sldc_unit_generation', 'source_ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS sldc_unit_generation_station_ts
    ON sldc_unit_generation (station, unit, source_ts DESC);

-- Daily per-unit row counts, the liveness figure for this table.
CREATE OR REPLACE VIEW sldc_unit_daily_rows AS
SELECT
    (source_ts AT TIME ZONE 'Asia/Kolkata')::date        AS ist_date,
    count(*)                                             AS rows_written,
    count(*) FILTER (WHERE quality = 0)                  AS good_rows,
    count(DISTINCT (station, unit))                      AS units,
    count(DISTINCT source_ts)                            AS distinct_readings
FROM sldc_unit_generation
GROUP BY 1
ORDER BY 1 DESC;

-- How far the sum of the units may differ from the published station total
-- before the minute is flagged. This is configuration, not code: a row per
-- station, changed with an UPDATE.
--
-- The defaults are provisional. On the one captured page (22/09/2026 21:10
-- IST) five of the six stations differed from their unit sum by 1 to 13 MW
-- (at most 1.2 %), which is what totals and unit figures sampled at slightly
-- different instants look like. RTPS published a total of 1190 MW with all
-- eight units reading 0 — a genuine inconsistency on the page, and the kind of
-- thing this check exists to surface. The tolerance is the larger of the
-- absolute and the fractional figure.
CREATE TABLE IF NOT EXISTS sldc_consistency_tolerance (
    station         text                PRIMARY KEY,
    tolerance_mw    double precision    NOT NULL CHECK (tolerance_mw >= 0),
    tolerance_frac  double precision    NOT NULL CHECK (tolerance_frac >= 0)
);

INSERT INTO sldc_consistency_tolerance (station, tolerance_mw, tolerance_frac)
VALUES ('RTPS', 15, 0.02), ('BTPS', 15, 0.02), ('YTPS', 15, 0.02),
       ('SHARAVATHI', 15, 0.02), ('VARAHI', 15, 0.02), ('ALMATTI', 15, 0.02)
ON CONFLICT (station) DO NOTHING;

-- One row per station per page timestamp: the published total, the sum of the
-- units, and a verdict.
--
--   consistent    every unit and the total are Good and agree within tolerance
--   inconsistent  every unit and the total are Good and do NOT agree
--   incomplete    at least one unit, or the total, is not Good — the sum cannot
--                 be computed honestly, so no verdict is given. A Bad unit is
--                 never treated as 0 MW to make the arithmetic work.
--   no tolerance configured
--                 the station has no row in sldc_consistency_tolerance; said
--                 so, rather than judged against an invented default.
CREATE OR REPLACE VIEW sldc_unit_consistency AS
WITH units AS (
    SELECT station, source_ts,
           count(*)                                   AS units_recorded,
           count(*) FILTER (WHERE quality = 0)        AS units_good,
           sum(generation_mw) FILTER (WHERE quality = 0) AS unit_sum_mw
    FROM sldc_unit_generation
    GROUP BY station, source_ts
)
SELECT
    g.station,
    g.source_ts,
    g.generation_mw                                   AS station_total_mw,
    g.quality                                         AS total_quality,
    u.units_recorded,
    u.units_good,
    CASE WHEN u.units_good = u.units_recorded THEN u.unit_sum_mw END AS unit_sum_mw,
    CASE WHEN g.quality = 0 AND u.units_good = u.units_recorded
         THEN u.unit_sum_mw - g.generation_mw END     AS difference_mw,
    greatest(t.tolerance_mw, t.tolerance_frac * abs(g.generation_mw))
                                                      AS tolerance_mw,
    CASE
        WHEN g.quality <> 0 OR u.units_good <> u.units_recorded THEN 'incomplete'
        WHEN t.station IS NULL                                THEN 'no tolerance configured'
        WHEN abs(u.unit_sum_mw - g.generation_mw)
             <= greatest(t.tolerance_mw, t.tolerance_frac * abs(g.generation_mw))
             THEN 'consistent'
        ELSE 'inconsistent'
    END                                               AS verdict
FROM sldc_generation g
JOIN units u USING (station, source_ts)
LEFT JOIN sldc_consistency_tolerance t USING (station);

-- The daily figure: per station per IST day, how many page timestamps were
-- checked and how many were flagged.
CREATE OR REPLACE VIEW sldc_unit_consistency_daily AS
SELECT
    (source_ts AT TIME ZONE 'Asia/Kolkata')::date              AS ist_date,
    station,
    count(*)                                                   AS readings,
    count(*) FILTER (WHERE verdict = 'consistent')             AS consistent,
    count(*) FILTER (WHERE verdict = 'inconsistent')           AS inconsistent,
    count(*) FILTER (WHERE verdict = 'incomplete')             AS incomplete,
    max(abs(difference_mw))                                    AS max_abs_difference_mw
FROM sldc_unit_consistency
GROUP BY 1, 2
ORDER BY 1 DESC, 2;

COMMIT;
