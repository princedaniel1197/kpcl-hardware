-- Karnataka SLDC live generation recorder — schema.
--
-- This is the scraper's own schema. It is deliberately NOT part of the Stage 2
-- archive migrations: the scraper is not a stage of the build plan, and nothing
-- in the staged work depends on it. It shares the TimescaleDB instance and the
-- project's conventions, and nothing else.
--
-- Conventions followed here, from CLAUDE.md:
--   * (station, source_ts) is the primary key, so re-recording a reading we
--     already hold cannot create a duplicate. The page publishes a timestamp
--     with one-minute resolution and we poll every 60 s, so repeat readings of
--     an unchanged page are expected and are absorbed by the schema, not by
--     logic in the poller.
--   * source_ts is the page's own timestamp; server_ts is when we fetched it.
--     Both are stored. Neither is ever derived from the other.
--   * quality is the numeric OPC UA StatusCode. A value that could not be read
--     is stored as NULL with a Bad status and a reason, never as zero: several
--     of these stations legitimately generate 0 MW.

BEGIN;

CREATE TABLE IF NOT EXISTS sldc_generation (
    station         text                     NOT NULL,
    source_ts       timestamptz              NOT NULL,  -- the page's timestamp
    server_ts       timestamptz              NOT NULL,  -- when we fetched it
    generation_mw   double precision,                   -- NULL when not Good
    quality         bigint                   NOT NULL,  -- OPC UA StatusCode
    reason          text,                               -- why, when not Good
    PRIMARY KEY (station, source_ts)
);

SELECT create_hypertable('sldc_generation', 'source_ts', if_not_exists => TRUE);

-- System-wide quantities. Frequency is a property of the grid, not of any one
-- station, so it is stored once per reading rather than repeated on six rows.
CREATE TABLE IF NOT EXISTS sldc_system (
    source_ts           timestamptz          NOT NULL,
    server_ts           timestamptz          NOT NULL,
    frequency_hz        double precision,
    frequency_quality   bigint               NOT NULL,
    state_gen_mw        double precision,
    total_gen_mw        double precision,
    raw_page_timestamp  text                 NOT NULL,  -- exactly as printed
    PRIMARY KEY (source_ts)
);

SELECT create_hypertable('sldc_system', 'source_ts', if_not_exists => TRUE);

-- Every poll attempt, successful or not. This is what makes "the scraper is
-- alive but the page is down" distinguishable from "the scraper is dead".
CREATE TABLE IF NOT EXISTS sldc_poll_log (
    attempt_ts      timestamptz              NOT NULL,
    outcome         text                     NOT NULL,  -- ok | http_error | parse_error | db_error
    http_status     integer,
    rows_written    integer                  NOT NULL DEFAULT 0,
    page_source_ts  timestamptz,                        -- NULL if unreadable
    duration_ms     integer,
    detail          text,
    PRIMARY KEY (attempt_ts)
);

CREATE INDEX IF NOT EXISTS sldc_generation_station_ts
    ON sldc_generation (station, source_ts DESC);

-- Helper view decoding the numeric StatusCode for humans, per the project
-- convention. The numeric code remains the stored truth.
CREATE OR REPLACE VIEW sldc_generation_decoded AS
SELECT
    station,
    source_ts,
    server_ts,
    server_ts - source_ts AS page_age,
    generation_mw,
    quality,
    CASE
        WHEN quality = 0                        THEN 'Good'
        WHEN (quality & 3221225472) = 1073741824 THEN 'Uncertain'
        WHEN (quality & 3221225472) = 2147483648 THEN 'Bad'
        ELSE 'Unknown'
    END AS quality_class,
    reason
FROM sldc_generation;

-- Daily row counts, so liveness is one query away.
CREATE OR REPLACE VIEW sldc_daily_rows AS
SELECT
    (source_ts AT TIME ZONE 'Asia/Kolkata')::date        AS ist_date,
    count(*)                                             AS rows_written,
    count(*) FILTER (WHERE quality = 0)                  AS good_rows,
    count(DISTINCT station)                              AS stations,
    count(DISTINCT source_ts)                            AS distinct_readings,
    min(source_ts)                                       AS first_reading,
    max(source_ts)                                       AS last_reading
FROM sldc_generation
GROUP BY 1
ORDER BY 1 DESC;

COMMIT;
