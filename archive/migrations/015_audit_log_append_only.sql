-- The audit trail is append-only. (§433)
--
-- An audit log that can be edited records what someone later decided it should
-- say. Found during the review of 23 September 2026: the access-control test
-- fixture deleted the audit rows its tests had written, to leave the database
-- tidy. Tidy is not the point of an audit trail. From here the database itself
-- refuses UPDATE, DELETE and TRUNCATE on audit_log, for every role that is not
-- a superuser bypassing triggers deliberately.

BEGIN;

CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;

CREATE TRIGGER audit_log_no_update_or_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();

COMMIT;
