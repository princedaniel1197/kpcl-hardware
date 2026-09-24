-- A standing statement about a tag's measurement quality, and an "uncertain"
-- health state to show it.
--
-- 24 September 2026: the bench rig's current reading (RIG_CURRENT) was found,
-- with a meter, to be off by up to a factor of four, through an uncalibrated
-- ADC and a bad ground; the decision was to leave the hardware as it is and
-- treat the reading as permanently unverified. The bridge publishes it as
-- UncertainSensorCalibration; this column holds the reason in words, which the
-- engine carries into tag_health.detail and the dashboard shows.
--
-- A tag with a quality_note must not be an input to a KPI, an alert or an
-- event template; engine/test_quality_note.py asserts it against config.

BEGIN;

ALTER TABLE tag ADD COLUMN quality_note text;

COMMENT ON COLUMN tag.quality_note IS
    'A standing limitation of this tag''s measurement, shown wherever its health '
    'is shown (e.g. "uncalibrated - bench demo only"). NULL: none recorded. Set '
    'from config/unit1_tags.json through collector.seed, so every change is in '
    'audit_log.';

-- quality_class(NULL) fell through to ELSE and said 'Reserved', so a tag with
-- no samples at all showed on the dashboard as "Reserved ()". No StatusCode is
-- not a Reserved StatusCode: STRICT makes it NULL.
CREATE OR REPLACE FUNCTION quality_class(code bigint) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT CASE (code >> 30) & 3
        WHEN 0 THEN 'Good'
        WHEN 1 THEN 'Uncertain'
        WHEN 2 THEN 'Bad'
        ELSE 'Reserved'
    END
$$;

-- 'uncertain' when the source says Uncertain: the value is kept and shown, and
-- it is not 'ok'. Ordered after every Bad-type state, so it never hides one.
CREATE OR REPLACE VIEW tag_health_decoded AS
 SELECT t.name AS tag_name,
    h.tag_id,
    h.evaluated_at,
    h.last_source_ts,
    h.is_bad,
    h.is_stale,
    h.is_frozen,
    h.is_missing,
    h.is_out_of_range,
    h.is_comm_failed,
    h.source_quality,
    h.computed_quality,
    h.detail,
    quality_class(h.source_quality) AS source_class,
    quality_class(h.computed_quality) AS computed_class,
        CASE
            WHEN h.is_missing THEN 'missing'::text
            WHEN h.is_comm_failed THEN 'comm_failed'::text
            WHEN h.is_bad THEN 'bad'::text
            WHEN h.is_stale THEN 'stale'::text
            WHEN h.is_frozen THEN 'frozen'::text
            WHEN h.is_out_of_range THEN 'out_of_range'::text
            WHEN quality_class(h.source_quality) = 'Uncertain' THEN 'uncertain'::text
            ELSE 'ok'::text
        END AS worst_state,
    t.quality_note
   FROM tag_health h
     JOIN tag t ON t.id = h.tag_id;

COMMIT;
