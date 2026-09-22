-- Template composition and tag resolution. (§341, §392, Stage 5)
--
-- 001 gave templates a parent_template_id, which expresses DERIVATION: a
-- 210 MW unit specialising a generic unit. That is not the same relationship as
-- COMPOSITION: a unit containing a boiler, a turbine and a generator. Both are
-- needed, so composition gets its own table rather than being crammed into the
-- same column.
--
-- The point of all this is rule 7: adding an asset must be configuration, not
-- code. Creating Unit 2 should mean instantiating a template, and everything
-- underneath it -- child elements, attributes, and the tag rows the attributes
-- resolve to -- should follow from the template. If any of that required
-- editing Python, the asset model would be wrong.

BEGIN;

-- What an element instance knows about itself, e.g. {"unit": "U2"}. Tag
-- patterns are resolved against this, so the same template produces U1_MW for
-- one element and U2_MW for another.
ALTER TABLE element ADD COLUMN context jsonb NOT NULL DEFAULT '{}'::jsonb;

-- How an attribute finds its tag, e.g. "{unit}_MS_TEMP". Held on the template
-- so the mapping is stated once per class of asset, not once per instance.
ALTER TABLE attribute_template ADD COLUMN tag_pattern text;

-- Enough tag metadata for instantiation to create a tag row that does not yet
-- exist. Without this, adding a unit would still need somebody to write the
-- tag rows by hand, which is the thing rule 7 forbids.
ALTER TABLE attribute_template ADD COLUMN range_low double precision;
ALTER TABLE attribute_template ADD COLUMN range_high double precision;
ALTER TABLE attribute_template ADD COLUMN scan_rate_ms integer;
ALTER TABLE attribute_template ADD COLUMN exc_dev double precision;
ALTER TABLE attribute_template ADD COLUMN comp_dev double precision;
ALTER TABLE attribute_template ADD COLUMN max_time_ms integer;
ALTER TABLE attribute_template ADD COLUMN source_system text;

CREATE TABLE template_composition (
    id                 bigserial PRIMARY KEY,
    parent_template_id bigint  NOT NULL REFERENCES element_template(id) ON DELETE CASCADE,
    child_template_id  bigint  NOT NULL REFERENCES element_template(id),
    name               text    NOT NULL,   -- name of the child element created
    code_suffix        text    NOT NULL,   -- appended to the parent asset code
    level              text,               -- ISA-95 level of the child
    sort_order         integer NOT NULL DEFAULT 0,
    UNIQUE (parent_template_id, name),
    CHECK (parent_template_id <> child_template_id)
);

CREATE INDEX template_composition_parent ON template_composition (parent_template_id);

-- The ISA-95 level an element of this template sits at.
ALTER TABLE element_template ADD COLUMN level text;

COMMIT;
