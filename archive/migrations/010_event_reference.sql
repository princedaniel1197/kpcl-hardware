-- Reference events for milestone comparison. (§475, Stage 8)
--
-- Comparison needs something to compare against. Two things, per the clause:
-- a stored reference curve, and the previous best event of the same template.
-- The reference is a real captured event marked as the benchmark, rather than a
-- separate table of idealised numbers, so that what a start-up is measured
-- against is a start-up that actually happened.

BEGIN;

ALTER TABLE event_frame ADD COLUMN is_reference boolean NOT NULL DEFAULT false;
ALTER TABLE event_frame ADD COLUMN notes text;

-- At most one reference per (template, element).
CREATE UNIQUE INDEX event_frame_one_reference
    ON event_frame (element_id, template) WHERE is_reference;

-- Milestones are ordered within a template; the order is a property of the
-- sequence, not of the row's id.
ALTER TABLE event_milestone ADD COLUMN sort_order integer NOT NULL DEFAULT 0;

CREATE OR REPLACE VIEW event_frame_milestones AS
SELECT
    f.id              AS event_frame_id,
    f.template,
    e.asset_code,
    f.start_ts,
    f.end_ts,
    f.status,
    f.is_reference,
    EXTRACT(epoch FROM (f.end_ts - f.start_ts))::double precision AS duration_s,
    m.name            AS milestone,
    m.sort_order,
    m.ts              AS milestone_ts,
    EXTRACT(epoch FROM (m.ts - f.start_ts))::double precision AS offset_s,
    m.value,
    m.quality
FROM event_frame f
JOIN element e ON e.id = f.element_id
LEFT JOIN event_milestone m ON m.event_frame_id = f.id
ORDER BY f.start_ts DESC, m.sort_order;

COMMIT;
