-- KPI definitions and computed values. (§320, §378, §379, §381, §383)
--
-- Every definition is versioned and every computed value records the version
-- that produced it, so any historical result traces to the exact equation used.

BEGIN;

CREATE TABLE kpi_definition (
    id                bigserial PRIMARY KEY,
    name              text        NOT NULL,
    description       text,
    -- measured | calculated | derived_statistical | ai_ml  (§378)
    classification    text        NOT NULL,
    equation          text        NOT NULL,
    inputs            jsonb       NOT NULL DEFAULT '[]'::jsonb,
    constants         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    engineering_unit  text,
    reference_value   double precision,
    validity_low      double precision,
    validity_high     double precision,
    -- What to do with a Bad input. The only value this project accepts is
    -- 'propagate' -- a KPI with a Bad input returns Bad with a reason. The
    -- column exists because the tender asks for it to be declared, not because
    -- substitution is an option. (§318)
    bad_data_treatment text       NOT NULL DEFAULT 'propagate',
    calculation_freq_ms integer   NOT NULL DEFAULT 60000,
    version           integer     NOT NULL,
    valid_from        timestamptz NOT NULL DEFAULT now(),
    valid_to          timestamptz,
    created_by        text,
    UNIQUE (name, version),
    CHECK (classification IN ('measured','calculated','derived_statistical','ai_ml')),
    CHECK (bad_data_treatment IN ('propagate')),
    CHECK (version > 0),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- Only one version of a KPI may be current at a time.
CREATE UNIQUE INDEX kpi_definition_one_current
    ON kpi_definition (name) WHERE valid_to IS NULL;

CREATE TABLE kpi_value (
    kpi_definition_id bigint      NOT NULL REFERENCES kpi_definition(id),
    -- Denormalised deliberately: the version is part of the result's identity,
    -- so a later edit to the definition row can never silently restate history.
    kpi_version       integer     NOT NULL,
    element_id        bigint      NOT NULL REFERENCES element(id),
    ts                timestamptz NOT NULL,
    -- NULL when the result is Bad. A KPI with a Bad input does not return zero,
    -- the last good value, or an interpolation. (§318)
    value             double precision,
    quality           bigint      NOT NULL,
    -- Names the offending input when the result is not Good.
    reason            text,
    PRIMARY KEY (kpi_definition_id, element_id, ts)
);

SELECT create_hypertable('kpi_value', 'ts', chunk_time_interval => INTERVAL '7 days');

CREATE INDEX kpi_value_element_ts ON kpi_value (element_id, ts DESC);

CREATE OR REPLACE VIEW kpi_value_decoded AS
SELECT
    k.ts, d.name AS kpi_name, k.kpi_version, e.asset_code, e.name AS element_name,
    k.value, k.quality, quality_class(k.quality) AS quality_class, k.reason,
    d.engineering_unit, d.classification
FROM kpi_value k
JOIN kpi_definition d ON d.id = k.kpi_definition_id
JOIN element e ON e.id = k.element_id;

COMMIT;
