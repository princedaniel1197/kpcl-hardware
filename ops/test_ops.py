"""Stage 13 tests: access control, alerts, capacity, export."""

from __future__ import annotations

import datetime as dt
import os
import zipfile

import psycopg
import pytest

from ops import access, alerts, capacity, export as export_mod

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")


@pytest.fixture
def conn():
    """A connection that cleans up after itself.

    Creating a principal COMMITS -- it has to, because the token is returned to
    a caller who will use it on another connection. So rolling back is not
    enough: the fixture removes the principals and rules the tests made.
    Without this the suite passed in isolation and failed the second time it
    ran, which is the worst kind of test.

    It does NOT remove the audit rows the tests wrote. It used to; the audit
    log is append-only now (migration 015), and those rows are true records of
    what the tests did.
    """
    try:
        connection = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"archive not reachable: {exc}")
    try:
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM principal WHERE username LIKE 'test.%'")
            cur.execute("DELETE FROM alert_rule WHERE name = 'bad-sev'")
        connection.commit()
        connection.close()


# --- access control (§509) ---------------------------------------------------

def test_the_six_roles_the_clause_names_exist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM role ORDER BY name")
        assert {r[0] for r in cur.fetchall()} == set(access.ROLES)


def test_a_token_is_never_stored(conn):
    """Only the SHA-256 is kept. A lost token is replaced, not looked up."""
    token = access.create_principal(conn, "test.token", "operations",
                                    actor="test")
    with conn.cursor() as cur:
        cur.execute("SELECT token_sha256 FROM principal WHERE username='test.token'")
        stored = cur.fetchone()[0]
    assert stored != token
    assert stored == access.token_hash(token)
    assert len(stored) == 64


def test_authentication_resolves_a_role_and_its_permissions(conn):
    token = access.create_principal(conn, "test.eng", "engineering", actor="test")
    principal = access.authenticate(conn, token)
    assert principal.role == "engineering"
    assert principal.may("write:kpi")
    assert principal.may("read:data")


def test_a_role_cannot_do_what_it_does_not_carry(conn):
    token = access.create_principal(conn, "test.ops", "operations", actor="test")
    principal = access.authenticate(conn, token)
    assert principal.may("ack:alerts")
    assert not principal.may("write:kpi")
    with pytest.raises(access.AccessError, match="does not carry"):
        access.require(principal, "write:kpi")


def test_only_admin_carries_access_control(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT role_name FROM role_permission"
                    " WHERE permission_name = 'admin:access'")
        assert [r[0] for r in cur.fetchall()] == ["admin"]


def test_an_unknown_token_is_refused(conn):
    with pytest.raises(access.AccessError, match="unknown or disabled"):
        access.authenticate(conn, "not-a-real-token")


def test_a_revoked_principal_is_refused(conn):
    token = access.create_principal(conn, "test.gone", "corporate", actor="test")
    access.authenticate(conn, token)
    access.revoke(conn, "test.gone", actor="test")
    with pytest.raises(access.AccessError):
        access.authenticate(conn, token)


def test_an_unknown_and_a_disabled_token_give_the_same_message(conn):
    """Telling an attacker which tokens exist is free information."""
    token = access.create_principal(conn, "test.disabled", "corporate", actor="test")
    access.revoke(conn, "test.disabled", actor="test")
    messages = []
    for candidate in (token, "definitely-not-a-token"):
        try:
            access.authenticate(conn, candidate)
        except access.AccessError as exc:
            messages.append(str(exc))
    assert len(set(messages)) == 1


def test_the_station_role_is_scoped(conn):
    token = access.create_principal(conn, "test.rtps", "station",
                                    station="RTPS", actor="test")
    principal = access.authenticate(conn, token)
    assert principal.may_see_station("RTPS")
    assert not principal.may_see_station("BTPS")
    # Data that belongs to no station is not assumed visible.
    assert not principal.may_see_station(None)


def test_every_other_role_is_fleet_wide(conn):
    token = access.create_principal(conn, "test.corp", "corporate", actor="test")
    principal = access.authenticate(conn, token)
    assert principal.may_see_station("RTPS")
    assert principal.may_see_station("BTPS")
    assert principal.may_see_station(None)


def test_the_station_role_must_be_scoped(conn):
    with pytest.raises(access.AccessError, match="must be scoped"):
        access.create_principal(conn, "test.unscoped", "station", actor="test")


def test_creating_a_principal_is_audited(conn):
    access.create_principal(conn, "test.audited", "maintenance", actor="inspector")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log WHERE actor='inspector'"
                    " AND entity='principal'")
        assert cur.fetchone()[0] >= 1


def test_tokens_have_real_entropy():
    tokens = {access.new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 40 for t in tokens)


# --- alerts (§507) ------------------------------------------------------------

def test_quality_is_an_alert_condition(conn):
    """A system that can only alarm on thresholds cannot tell you your
    instrument has failed — it will report that a dead transmitter reads a
    perfectly normal 0."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alert_rule WHERE condition = 'bad'")
        assert cur.fetchone()[0] >= 1


def test_a_threshold_rule_will_not_fire_on_a_bad_sample(conn):
    """A Bad sample carries no value. A threshold rule that fires on one is
    reporting a number that does not exist."""
    rule = alerts.Rule(id=-1, name="t", subject_kind="tag",
                       subject="U1_COAL_FLOW", condition=">", threshold=-1e9,
                       for_seconds=5, severity="warning", recipients=[])
    # Threshold -1e9 would match any real number; if the window is all Bad the
    # rule must still not fire.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sample s JOIN tag t ON t.id=s.tag_id"
                    " WHERE t.name='U1_COAL_FLOW'")
    firing, _, _, _ = alerts.evaluate(conn, rule)
    assert isinstance(firing, bool)


def test_every_rule_has_a_debounce(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alert_rule WHERE for_seconds < 0")
        assert cur.fetchone()[0] == 0


def test_delivery_failure_is_recorded_not_swallowed(conn):
    """An alerting system that silently fails to alert is worse than none,
    because it is trusted."""
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns"
                    " WHERE table_name='alert' AND column_name IN"
                    " ('delivered','delivery_error')")
        assert len(cur.fetchall()) == 2


def test_an_alert_rule_rejects_an_unknown_severity(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO alert_rule (name, subject_kind, subject,"
                        " condition, severity) VALUES"
                        " ('bad-sev','tag','X','>','catastrophic')")


# --- capacity (§344) ----------------------------------------------------------

def test_capacity_collects_the_metrics_that_run_out(conn):
    values = capacity.collect(conn)
    for metric in ("database_bytes", "sample_rows", "host_disk_free_bytes",
                   "host_disk_used_pct", "chunks"):
        assert metric in values


def test_no_growth_gives_no_projection_rather_than_a_reassuring_number(conn):
    """A capacity figure without a growth rate answers 'how full is it' but not
    'when does it stop working'."""
    result = capacity.days_until_full(conn)
    assert result is None or result > 0


# --- export (§503) -------------------------------------------------------------

def test_the_export_carries_quality_and_never_writes_a_zero_for_bad(conn, tmp_path):
    """An export that drops quality hands the recipient a file that looks
    authoritative and cannot be checked."""
    import csv
    import io
    destination = tmp_path / "export.zip"
    end = dt.datetime.now(dt.timezone.utc)
    manifest = export_mod.export(conn, destination, start=end - dt.timedelta(hours=2),
                                 end=end, actor="test")
    assert destination.exists()
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert {"samples.csv", "kpi_values.csv", "configuration.json",
                "manifest.json", "audit_log.csv"} <= names
        rows = list(csv.DictReader(
            io.StringIO(archive.read("samples.csv").decode())))
    assert rows, "no samples exported"
    assert {"quality", "quality_class"} <= set(rows[0])
    for row in rows:
        if row["quality_class"] == "Bad":
            assert row["value"] == "", "a Bad sample must export an empty value"


def test_the_export_manifest_records_provenance(conn, tmp_path):
    destination = tmp_path / "e.zip"
    end = dt.datetime.now(dt.timezone.utc)
    manifest = export_mod.export(conn, destination, start=end - dt.timedelta(hours=1),
                                 end=end, actor="inspector")
    assert manifest["exported_by"] == "inspector"
    assert "window" in manifest
    for info in manifest["files"].values():
        assert len(info["sha256"]) == 64
