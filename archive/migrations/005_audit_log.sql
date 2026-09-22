-- Audit trail on every configuration change: actor, timestamp, old value,
-- new value and reason. (§433)

BEGIN;

CREATE TABLE audit_log (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    actor      text        NOT NULL,
    entity     text        NOT NULL,   -- 'tag', 'kpi_definition', 'element'
    entity_id  text        NOT NULL,
    field      text,
    old_value  text,
    new_value  text,
    reason     text
);

CREATE INDEX audit_log_ts ON audit_log (ts DESC);
CREATE INDEX audit_log_entity ON audit_log (entity, entity_id, ts DESC);

COMMIT;
