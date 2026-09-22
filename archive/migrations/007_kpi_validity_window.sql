-- Allow a KPI definition to be superseded at the instant it was created.
--
-- 004 required valid_to > valid_from. That forbids a zero-length validity
-- window, which makes correcting a mistaken definition inside one transaction
-- impossible: PostgreSQL's now() is transaction-start time, so opening and
-- closing a version together gives valid_to = valid_from.
--
-- A zero-length window is meaningful rather than malformed -- it records a
-- definition that never produced a value -- so the constraint is relaxed to
-- >=. Ordering is still enforced: valid_to may not precede valid_from.

BEGIN;

ALTER TABLE kpi_definition DROP CONSTRAINT kpi_definition_check;
ALTER TABLE kpi_definition ADD CONSTRAINT kpi_definition_validity_window
    CHECK (valid_to IS NULL OR valid_to >= valid_from);

COMMIT;
