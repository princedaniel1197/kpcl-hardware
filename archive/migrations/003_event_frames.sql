-- Event frames and their milestones. (§472, §475, Stage 8)

BEGIN;

CREATE TABLE event_frame (
    id          bigserial PRIMARY KEY,
    template    text        NOT NULL,          -- 'ThermalStartup', 'Shutdown'
    element_id  bigint      NOT NULL REFERENCES element(id),
    start_ts    timestamptz NOT NULL,
    end_ts      timestamptz,                   -- NULL while the event is open
    status      text        NOT NULL DEFAULT 'open',
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (end_ts IS NULL OR end_ts >= start_ts),
    CHECK (status IN ('open', 'closed', 'aborted'))
);

CREATE INDEX event_frame_element_start ON event_frame (element_id, start_ts DESC);
CREATE INDEX event_frame_template ON event_frame (template, start_ts DESC);

-- Only one event of a template may be open on an element at a time.
CREATE UNIQUE INDEX event_frame_one_open
    ON event_frame (element_id, template) WHERE end_ts IS NULL;

CREATE TABLE event_milestone (
    id             bigserial PRIMARY KEY,
    event_frame_id bigint      NOT NULL REFERENCES event_frame(id) ON DELETE CASCADE,
    name           text        NOT NULL,       -- 'boiler_lightup', 'synchronisation'
    ts             timestamptz NOT NULL,
    value          double precision,
    -- A milestone derived from a Bad input is not a milestone. Quality travels
    -- here too, for the same reason it travels everywhere else.
    quality        bigint      NOT NULL DEFAULT 0,
    UNIQUE (event_frame_id, name)
);

CREATE INDEX event_milestone_frame ON event_milestone (event_frame_id, ts);

-- Summary statistics over the event window, per tag. (Stage 8)
CREATE TABLE event_frame_summary (
    event_frame_id bigint NOT NULL REFERENCES event_frame(id) ON DELETE CASCADE,
    tag_id         bigint NOT NULL REFERENCES tag(id),
    min_value      double precision,
    max_value      double precision,
    mean_value     double precision,
    -- How the summary was reached matters as much as the number: a mean over a
    -- window that was mostly Bad is not comparable to one that was not.
    sample_count   integer NOT NULL,
    good_count     integer NOT NULL,
    PRIMARY KEY (event_frame_id, tag_id)
);

COMMIT;
