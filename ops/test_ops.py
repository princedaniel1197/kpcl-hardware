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
    """Looked up by the new principal's own id: the audit log is append-only,
    so counting every row an actor ever wrote would pass for ever after the
    first run."""
    access.create_principal(conn, "test.audited", "maintenance", actor="inspector")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM principal WHERE username='test.audited'")
        principal_id = cur.fetchone()[0]
        cur.execute("SELECT actor, field, new_value, reason FROM audit_log"
                    " WHERE entity='principal' AND entity_id=%s",
                    (str(principal_id),))
        assert cur.fetchall() == [("inspector", "role", "maintenance",
                                   "created principal test.audited")]


def test_tokens_have_real_entropy():
    tokens = {access.new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 40 for t in tokens)


# --- alerts (§507) ------------------------------------------------------------

NOW = dt.datetime(2031, 3, 1, 12, 0, tzinfo=dt.timezone.utc)
BAD = 2156593152


def _tag_with(conn, samples):
    """A throwaway tag with (seconds before NOW, value, quality) samples, inside
    the fixture's transaction."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tag (name) VALUES ('TEST_ALERT_TAG') RETURNING id")
        tag_id = cur.fetchone()[0]
        for ago, value, quality in samples:
            ts = NOW - dt.timedelta(seconds=ago)
            cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                        " quality) VALUES (%s,%s,%s,%s,%s)",
                        (tag_id, ts, ts + dt.timedelta(milliseconds=30), value,
                         quality))
    return "TEST_ALERT_TAG"


def _rule(subject, condition, threshold=None, for_seconds=30):
    return alerts.Rule(id=-1, name="t", subject_kind="tag", subject=subject,
                       condition=condition, threshold=threshold,
                       for_seconds=for_seconds, severity="warning", recipients=[])


def test_a_tag_that_stays_bad_keeps_its_alert(conn):
    """Acquisition reports by exception: a tag that went Bad a minute ago and
    stayed Bad has no sample inside a 30 s window. Its state carries in. The
    first version looked only inside the window, and the alert raised and
    cleared on alternate cycles."""
    tag = _tag_with(conn, [(90, 5.0, 0), (60, None, BAD)])
    firing, detail, _, quality = alerts.evaluate(conn, _rule(tag, "bad"), NOW)
    assert firing and quality == BAD and "Bad" in detail


def test_a_tag_that_recovered_inside_the_window_does_not_alarm(conn):
    tag = _tag_with(conn, [(60, None, BAD), (10, 5.0, 0)])
    assert not alerts.evaluate(conn, _rule(tag, "bad"), NOW)[0]


def test_a_threshold_rule_will_not_fire_on_a_bad_sample(conn):
    """A Bad sample carries no value. A threshold rule that fires on one is
    reporting a number that does not exist -- so a threshold every real number
    exceeds must not fire while the tag is Bad, and must fire when it is Good."""
    bad = _tag_with(conn, [(60, None, BAD), (20, None, BAD)])
    assert not alerts.evaluate(conn, _rule(bad, ">", -1e9), NOW)[0]
    with conn.cursor() as cur:
        # Bounded by time, so only the chunk holding these samples is touched.
        # Without the bound the join reached every chunk, and once the archive's
        # older chunks were compressed the delete asked TimescaleDB to
        # decompress ten million rows, which it refuses.
        cur.execute("DELETE FROM sample WHERE source_ts BETWEEN %s AND %s AND"
                    " tag_id = (SELECT id FROM tag WHERE name = 'TEST_ALERT_TAG')",
                    (NOW - dt.timedelta(hours=1), NOW))
        cur.execute("DELETE FROM tag WHERE name = 'TEST_ALERT_TAG'")
    good = _tag_with(conn, [(60, 3.0, 0), (20, 4.0, 0)])
    firing, detail, value, _ = alerts.evaluate(conn, _rule(good, ">", -1e9), NOW)
    assert firing and value == 4.0


def test_stale_means_nothing_arrived_in_the_window(conn):
    tag = _tag_with(conn, [(120, 5.0, 0)])
    assert alerts.evaluate(conn, _rule(tag, "stale"), NOW)[0]
    assert not alerts.evaluate(conn, _rule(tag, "stale", for_seconds=300), NOW)[0]


def test_every_rule_has_a_debounce(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alert_rule WHERE for_seconds < 0")
        assert cur.fetchone()[0] == 0


def test_delivery_failure_is_recorded_not_swallowed(conn, monkeypatch):
    """An alerting system that silently fails to alert is worse than none,
    because it is trusted. An SMTP server that refuses the connection must
    leave the reason on the alert row."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO alert_rule (name, subject_kind, subject,"
                    " condition, severity, recipients) VALUES ('test.delivery',"
                    " 'tag','X','bad','warning', ARRAY['ops@example.invalid'])"
                    " RETURNING id")
        rule_id = cur.fetchone()[0]
        cur.execute("INSERT INTO alert (rule_id, severity, detail) VALUES"
                    " (%s,'warning','test') RETURNING id", (rule_id,))
        alert_id = cur.fetchone()[0]
    rule = alerts.Rule(rule_id, "test.delivery", "tag", "X", "bad", None, 0,
                       "warning", ["ops@example.invalid"])
    try:
        monkeypatch.setenv("CRPMS_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("CRPMS_SMTP_PORT", "1")        # nothing listens
        alerts.deliver(conn, alert_id, rule, "test")
        with conn.cursor() as cur:
            cur.execute("SELECT delivered, delivery_error FROM alert WHERE id=%s",
                        (alert_id,))
            delivered, error = cur.fetchone()
        assert delivered is not True and error
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alert WHERE rule_id = %s", (rule_id,))
            cur.execute("DELETE FROM alert_rule WHERE id = %s", (rule_id,))
        conn.commit()


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


def test_growth_is_measured_between_the_first_and_last_sample(conn):
    now = dt.datetime.now(dt.timezone.utc)
    with conn.cursor() as cur:
        for hours_ago, value in ((12, 1000.0), (6, 1600.0), (0, 2200.0)):
            cur.execute("INSERT INTO capacity_sample (ts, metric, value)"
                        " VALUES (%s, 'test_metric', %s)",
                        (now - dt.timedelta(hours=hours_ago), value))
    rate = capacity.growth(conn, "test_metric")
    assert rate["per_day"] == pytest.approx(2400.0, rel=1e-3)   # 1200 in 12 h


def test_no_growth_gives_no_projection_rather_than_a_reassuring_number(
        conn, monkeypatch):
    """A capacity figure without a growth rate answers 'how full is it' but not
    'when does it stop working' -- and a shrinking or flat database must give
    no projection, not a very large one."""
    for per_day in (0.0, -5.0):
        monkeypatch.setattr(capacity, "growth", lambda *a, **k: {"per_day": per_day})
        assert capacity.days_until_full(conn) is None
    monkeypatch.setattr(capacity, "growth", lambda *a, **k: {"per_day": 1e9})
    days = capacity.days_until_full(conn)
    assert days is not None and 0 < days < 1e7


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
