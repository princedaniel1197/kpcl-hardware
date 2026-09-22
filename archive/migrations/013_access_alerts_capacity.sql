-- Stage 13: the remaining named requirements.
--   role-based access (§509), alerts (§507), capacity monitoring (§344)
-- The audit trail (§433) already exists from migration 005 and is used
-- throughout; this adds the tables the other three need.

BEGIN;

-- ---------------------------------------------------------------------------
-- Role-based access (§509)
--
-- Six roles, as the clause names them. Permissions are per role and are data,
-- so granting one is a row rather than a deployment.
--
-- WHAT THIS IS NOT: an identity system. There is no password policy, no
-- lockout, no SSO, no session management. Principals authenticate with a
-- bearer token whose SHA-256 is stored, never the token. A real deployment
-- puts this behind the customer's directory; the point here is that every API
-- call carries a role and that the role decides what it may do.
-- ---------------------------------------------------------------------------

CREATE TABLE role (
    name        text PRIMARY KEY,
    description text NOT NULL
);

INSERT INTO role (name, description) VALUES
    ('corporate',   'Fleet-wide read across all stations'),
    ('station',     'Read for one station'),
    ('engineering', 'Read all; change KPI definitions, quality rules and asset model'),
    ('operations',  'Read all; acknowledge alerts'),
    ('maintenance', 'Read all; change tag configuration and alert rules'),
    ('admin',       'Everything, including access control');

CREATE TABLE permission (
    name        text PRIMARY KEY,
    description text NOT NULL
);

INSERT INTO permission (name, description) VALUES
    ('read:data',        'Read samples, trends and event frames'),
    ('read:config',      'Read tag, asset and KPI configuration'),
    ('write:tags',       'Change tag configuration'),
    ('write:assets',     'Change the asset model'),
    ('write:kpi',        'Change KPI definitions'),
    ('write:quality',    'Change data quality rules'),
    ('write:alerts',     'Change alert rules'),
    ('ack:alerts',       'Acknowledge alerts'),
    ('admin:access',     'Manage principals and roles'),
    ('export:data',      'Export data and configuration');

CREATE TABLE role_permission (
    role_name       text NOT NULL REFERENCES role(name) ON DELETE CASCADE,
    permission_name text NOT NULL REFERENCES permission(name) ON DELETE CASCADE,
    PRIMARY KEY (role_name, permission_name)
);

INSERT INTO role_permission (role_name, permission_name) VALUES
    ('corporate','read:data'), ('corporate','read:config'), ('corporate','export:data'),
    ('station','read:data'), ('station','read:config'),
    ('operations','read:data'), ('operations','read:config'), ('operations','ack:alerts'),
    ('engineering','read:data'), ('engineering','read:config'),
    ('engineering','write:kpi'), ('engineering','write:quality'),
    ('engineering','write:assets'), ('engineering','export:data'),
    ('maintenance','read:data'), ('maintenance','read:config'),
    ('maintenance','write:tags'), ('maintenance','write:alerts'),
    ('maintenance','ack:alerts'),
    ('admin','read:data'), ('admin','read:config'), ('admin','write:tags'),
    ('admin','write:assets'), ('admin','write:kpi'), ('admin','write:quality'),
    ('admin','write:alerts'), ('admin','ack:alerts'), ('admin','admin:access'),
    ('admin','export:data');

CREATE TABLE principal (
    id          bigserial PRIMARY KEY,
    username    text        NOT NULL UNIQUE,
    full_name   text,
    role_name   text        NOT NULL REFERENCES role(name),
    -- Scope for the 'station' role: which station this principal may read.
    station     text,
    -- SHA-256 of the bearer token. The token itself is never stored, and is
    -- shown once at creation.
    token_sha256 text       NOT NULL UNIQUE,
    enabled     boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz
);

CREATE VIEW principal_permissions AS
SELECT p.username, p.role_name, p.station, p.enabled, rp.permission_name
FROM principal p JOIN role_permission rp ON rp.role_name = p.role_name;

-- ---------------------------------------------------------------------------
-- Alerts (§507)
-- ---------------------------------------------------------------------------

CREATE TABLE alert_rule (
    id           bigserial PRIMARY KEY,
    name         text        NOT NULL UNIQUE,
    description  text,
    -- what to watch: a tag, a KPI, or a system condition
    subject_kind text        NOT NULL,
    subject      text        NOT NULL,
    -- how to decide
    condition    text        NOT NULL,
    threshold    double precision,
    -- how long it must hold, so a spike does not page anyone
    for_seconds  double precision NOT NULL DEFAULT 30,
    severity     text        NOT NULL DEFAULT 'warning',
    enabled      boolean     NOT NULL DEFAULT true,
    recipients   text[]      NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (subject_kind IN ('tag','kpi','quality','capacity','collector')),
    CHECK (condition IN ('>','>=','<','<=','==','!=','bad','uncertain','stale')),
    CHECK (severity IN ('info','warning','critical')),
    CHECK (for_seconds >= 0)
);

CREATE TABLE alert (
    id            bigserial PRIMARY KEY,
    rule_id       bigint      NOT NULL REFERENCES alert_rule(id) ON DELETE CASCADE,
    raised_at     timestamptz NOT NULL DEFAULT now(),
    cleared_at    timestamptz,
    acknowledged_at timestamptz,
    acknowledged_by text,
    severity      text        NOT NULL,
    detail        text        NOT NULL,
    value         double precision,
    quality       bigint,
    delivered     boolean     NOT NULL DEFAULT false,
    delivery_error text
);

CREATE INDEX alert_open ON alert (rule_id) WHERE cleared_at IS NULL;
CREATE INDEX alert_raised ON alert (raised_at DESC);

-- ---------------------------------------------------------------------------
-- Capacity monitoring (§344)
-- ---------------------------------------------------------------------------

CREATE TABLE capacity_sample (
    ts              timestamptz NOT NULL,
    metric          text        NOT NULL,
    value           double precision NOT NULL,
    unit            text,
    detail          text,
    PRIMARY KEY (ts, metric)
);

SELECT create_hypertable('capacity_sample', 'ts',
                         chunk_time_interval => INTERVAL '30 days');

COMMIT;
