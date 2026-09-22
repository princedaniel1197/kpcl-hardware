-- Asset framework: templates, elements, attributes. (§341, §392)
--
-- Adding an asset is configuration, not code. A second unit is a new element
-- created from an existing template; if adding a unit required editing Python,
-- the asset model would be wrong. That is what these tables are for.

BEGIN;

-- A template describes a class of thing: "Thermal Unit", "Boiler Feed Pump".
-- Templates derive from templates, so a 210 MW unit can specialise a generic
-- unit without restating it.
CREATE TABLE element_template (
    id                  bigserial PRIMARY KEY,
    name                text        NOT NULL UNIQUE,
    description         text,
    parent_template_id  bigint      REFERENCES element_template(id),
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE attribute_template (
    id                  bigserial PRIMARY KEY,
    element_template_id bigint      NOT NULL REFERENCES element_template(id) ON DELETE CASCADE,
    name                text        NOT NULL,
    description         text,
    engineering_unit    text,
    data_type           text        NOT NULL DEFAULT 'double',
    -- True when this attribute is expected to resolve to a live tag rather
    -- than hold a static value.
    is_tag_reference    boolean     NOT NULL DEFAULT true,
    default_value       text,
    UNIQUE (element_template_id, name)
);

-- The hierarchy: KPCL -> Station -> Unit -> System -> Sub-system -> Equipment
-- -> Component -> Parameter (§392). Self-referencing, so the depth is data.
CREATE TABLE element (
    id           bigserial PRIMARY KEY,
    -- A unique permanent asset code that is never reused. Deliberately not the
    -- surrogate key: the code outlives any particular row.
    asset_code   text        NOT NULL UNIQUE,
    name         text        NOT NULL,
    description  text,
    template_id  bigint      REFERENCES element_template(id),
    parent_id    bigint      REFERENCES element(id),
    level        text,       -- KPCL | Station | Unit | System | ...
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (id <> parent_id)
);

CREATE INDEX element_parent ON element (parent_id);
CREATE INDEX element_template_idx ON element (template_id);

CREATE TABLE attribute (
    id                    bigserial PRIMARY KEY,
    element_id            bigint NOT NULL REFERENCES element(id) ON DELETE CASCADE,
    attribute_template_id bigint REFERENCES attribute_template(id),
    name                  text   NOT NULL,
    engineering_unit      text,
    -- Exactly one of tag_id / static_value carries the attribute's meaning.
    -- tag_id is added in 002, once the tag table exists.
    static_value          text,
    UNIQUE (element_id, name)
);

COMMIT;
