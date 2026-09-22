-- Tags and the sample hypertable. (§335, §382, §460, §648)

BEGIN;

CREATE TABLE tag (
    id               bigserial PRIMARY KEY,
    name             text        NOT NULL UNIQUE,
    description      text,
    engineering_unit text,
    -- The instrument span. A tag without one cannot be range-checked, which
    -- Stage 6 depends on. (§436)
    range_low        double precision,
    range_high       double precision,
    source_system    text        NOT NULL DEFAULT 'unknown',
    scan_rate_ms     integer     NOT NULL DEFAULT 1000,
    -- Two-stage compression parameters. Convention is exc_dev ~ comp_dev / 2.
    -- (§442, Stage 4)
    exc_dev          double precision,
    comp_dev         double precision,
    max_time_ms      integer,
    element_id       bigint      REFERENCES element(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (range_high IS NULL OR range_low IS NULL OR range_high > range_low),
    CHECK (scan_rate_ms > 0)
);

CREATE INDEX tag_element ON tag (element_id);

-- An attribute resolves to a tag. Added here rather than in 001 because the
-- tag table did not exist yet.
ALTER TABLE attribute ADD COLUMN tag_id bigint REFERENCES tag(id);
CREATE INDEX attribute_tag ON attribute (tag_id);
ALTER TABLE attribute ADD CONSTRAINT attribute_tag_xor_static
    CHECK (NOT (tag_id IS NOT NULL AND static_value IS NOT NULL));

CREATE TABLE sample (
    tag_id    bigint           NOT NULL REFERENCES tag(id),

    -- When the value was produced. Never the time it was received, in any code
    -- path, including recovery and backfill. (§335)
    source_ts timestamptz      NOT NULL,

    -- When it went on the wire. A different quantity, always stored.
    server_ts timestamptz      NOT NULL,

    -- NULLABLE, deliberately. Measured against asyncua 2.0.1 in Stage 1: a
    -- DataValue whose StatusCode is Bad carries Value = None, VariantType Null,
    -- per OPC UA Part 4. A Bad sample therefore has no number to store, and
    -- storing 0 instead would be the exact substitution §318 forbids.
    value     double precision,

    -- The numeric OPC UA StatusCode. NOT a boolean, NOT a string.
    --
    -- bigint, not smallint. The build plan says smallint, but BadDeviceFailure
    -- is 2156593152, which exceeds smallint (32767) and int4 (2147483647) alike.
    -- The plan's own rule -- quality is the numeric StatusCode -- cannot be
    -- satisfied by a smallint column, so the rule wins and the type changes.
    quality   bigint           NOT NULL,

    -- Structural idempotency: replay can never duplicate, because the schema
    -- forbids it. (§382, §648)
    PRIMARY KEY (tag_id, source_ts)
);

SELECT create_hypertable('sample', 'source_ts', chunk_time_interval => INTERVAL '1 day');

-- Quality is stored numerically; this decodes it for humans. The OPC UA
-- severity lives in the top two bits: 00 Good, 01 Uncertain, 10 Bad.
CREATE OR REPLACE FUNCTION quality_class(code bigint) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE (code >> 30) & 3
        WHEN 0 THEN 'Good'
        WHEN 1 THEN 'Uncertain'
        WHEN 2 THEN 'Bad'
        ELSE 'Reserved'
    END
$$;

CREATE OR REPLACE VIEW sample_decoded AS
SELECT
    s.tag_id,
    t.name AS tag_name,
    s.source_ts,
    s.server_ts,
    s.server_ts - s.source_ts AS transit,
    s.value,
    s.quality,
    quality_class(s.quality) AS quality_class
FROM sample s
JOIN tag t ON t.id = s.tag_id;

COMMIT;
