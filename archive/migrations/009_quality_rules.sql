-- Data quality rules and tag health. (§384, §439, Stage 6)
--
-- THE POINT OF THIS MIGRATION is that computed quality is stored separately
-- from source quality and never overwrites it. A value that arrived Good but
-- failed a range check is not the same thing as a value that arrived Bad, and
-- the system must always be able to say which (§318, CLAUDE.md rule 2).
--
-- So sample.quality stays exactly as acquired, for ever, and everything this
-- migration adds is alongside it.

BEGIN;

CREATE TABLE quality_rule (
    id          bigserial PRIMARY KEY,
    tag_id      bigint  NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    rule_type   text    NOT NULL,
    -- Thresholds are per tag and per rule, held as data so retuning one is a
    -- row change rather than an edit to Python (§341).
    params      jsonb   NOT NULL DEFAULT '{}'::jsonb,
    enabled     boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tag_id, rule_type),
    CHECK (rule_type IN ('range', 'rate_of_change', 'cross_tag', 'frozen', 'stale'))
);

CREATE INDEX quality_rule_tag ON quality_rule (tag_id) WHERE enabled;

-- One row per (sample, rule) that failed. Absence of a row means the rule
-- passed; there is no need to store agreement.
CREATE TABLE quality_flag (
    tag_id           bigint      NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    source_ts        timestamptz NOT NULL,
    rule_type        text        NOT NULL,
    -- The COMPUTED StatusCode. The source StatusCode is in sample.quality and
    -- is not touched.
    computed_quality bigint      NOT NULL,
    reason           text        NOT NULL,
    detected_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tag_id, source_ts, rule_type)
);

SELECT create_hypertable('quality_flag', 'source_ts',
                         chunk_time_interval => INTERVAL '1 day');

-- Current tag health (§439). One row per tag, updated in place: this is state,
-- not history. The history is in quality_flag.
CREATE TABLE tag_health (
    tag_id            bigint      PRIMARY KEY REFERENCES tag(id) ON DELETE CASCADE,
    evaluated_at      timestamptz NOT NULL,
    last_source_ts    timestamptz,
    -- The six states §439 names. Independent booleans rather than one status
    -- column, because a tag can be several of these at once and collapsing
    -- them to one word loses which.
    is_bad            boolean     NOT NULL DEFAULT false,  -- arrived Bad
    is_stale          boolean     NOT NULL DEFAULT false,  -- nothing arriving
    is_frozen         boolean     NOT NULL DEFAULT false,  -- arriving, not moving
    is_missing        boolean     NOT NULL DEFAULT false,  -- never seen at all
    is_out_of_range   boolean     NOT NULL DEFAULT false,
    is_comm_failed    boolean     NOT NULL DEFAULT false,
    source_quality    bigint,                              -- as acquired
    computed_quality  bigint,                              -- worst rule verdict
    detail            text
);

-- Source and computed quality side by side. The whole argument of Stage 6 is
-- that these are different columns.
CREATE OR REPLACE VIEW sample_quality AS
SELECT
    s.tag_id,
    t.name AS tag_name,
    s.source_ts,
    s.value,
    s.quality                        AS source_quality,
    quality_class(s.quality)         AS source_class,
    f.rule_type,
    f.computed_quality,
    quality_class(f.computed_quality) AS computed_class,
    f.reason
FROM sample s
JOIN tag t ON t.id = s.tag_id
LEFT JOIN quality_flag f ON f.tag_id = s.tag_id AND f.source_ts = s.source_ts;

CREATE OR REPLACE VIEW tag_health_decoded AS
SELECT
    t.name AS tag_name, h.*,
    quality_class(h.source_quality)   AS source_class,
    quality_class(h.computed_quality) AS computed_class,
    CASE WHEN h.is_missing THEN 'missing'
         WHEN h.is_comm_failed THEN 'comm_failed'
         WHEN h.is_bad THEN 'bad'
         WHEN h.is_stale THEN 'stale'
         WHEN h.is_frozen THEN 'frozen'
         WHEN h.is_out_of_range THEN 'out_of_range'
         ELSE 'ok' END AS worst_state
FROM tag_health h JOIN tag t ON t.id = h.tag_id;

COMMIT;
