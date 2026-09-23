"""Configurable alerts with email delivery. (§507)

Rules are rows: what to watch, how to decide, how long it must hold, who to
tell. Adding an alert is configuration.

TWO DECISIONS WORTH STATING.

**Quality is a first-class alert condition.** A rule can fire on a tag going
Bad, not only on a tag crossing a number. A system that can only alarm on
thresholds cannot tell you your instrument has failed — it will happily report
that a dead transmitter is reading a perfectly normal 0.

**Email is off unless configured, and a failure to deliver is recorded rather
than swallowed.** An alerting system that silently fails to alert is worse than
none, because it is trusted. `alert.delivered` and `alert.delivery_error` say
what actually happened to each one.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import psycopg

log = logging.getLogger("ops.alerts")

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")

COMPARISONS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


@dataclass(frozen=True)
class Rule:
    id: int
    name: str
    subject_kind: str
    subject: str
    condition: str
    threshold: float | None
    for_seconds: float
    severity: str
    recipients: list[str]


def load_rules(conn: psycopg.Connection) -> list[Rule]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, subject_kind, subject, condition, threshold,"
            " for_seconds, severity, recipients FROM alert_rule WHERE enabled"
            " ORDER BY name")
        return [Rule(*r) for r in cur.fetchall()]


def _severity(code: int | None) -> int:
    return ((code or 0) >> 30) & 3


def evaluate(conn: psycopg.Connection, rule: Rule,
             now: dt.datetime | None = None) -> tuple[bool, str, float | None,
                                                       int | None]:
    """Decide whether a rule is in alarm. Returns (firing, detail, value, quality)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(seconds=max(rule.for_seconds, 1))

    if rule.subject_kind == "tag":
        # Acquisition reports by exception: a tag that goes Bad and stays Bad
        # sends nothing more until its max-time heartbeat. So the state that
        # holds across the window starts with the last sample BEFORE it, not
        # only the ones inside it. Looking only inside the window, an alert on a
        # tag that stayed Bad raised and cleared on alternate cycles, depending
        # on whether a heartbeat happened to land in it.
        with conn.cursor() as cur:
            cur.execute(
                "(SELECT s.value, s.quality, s.source_ts FROM sample s"
                "  JOIN tag t ON t.id = s.tag_id WHERE t.name = %s"
                "  AND s.source_ts < %s ORDER BY s.source_ts DESC LIMIT 1)"
                " UNION ALL"
                " (SELECT s.value, s.quality, s.source_ts FROM sample s"
                "  JOIN tag t ON t.id = s.tag_id WHERE t.name = %s"
                "  AND s.source_ts >= %s ORDER BY s.source_ts)",
                (rule.subject, since, rule.subject, since))
            rows = sorted(cur.fetchall(), key=lambda r: r[2])
        in_window = [r for r in rows if r[2] >= since]
        if rule.condition == "stale":
            # Stale is about arrival, so only the window counts.
            if not in_window:
                return (True, f"{rule.subject}: no sample for "
                        f"{rule.for_seconds:.0f}s", None, None)
            return False, "", in_window[-1][0], in_window[-1][1]
        if not rows:
            return False, "", None, None
        if rule.condition == "bad":
            # Must hold for the whole window: one Bad sample is not an alarm.
            if all(_severity(q) == 2 for _, q, _ in rows):
                return (True, f"{rule.subject} has been Bad for "
                        f"{rule.for_seconds:.0f}s", None, rows[-1][1])
            return False, "", None, None
        if rule.condition == "uncertain":
            if all(_severity(q) == 1 for _, q, _ in rows):
                return (True, f"{rule.subject} has been Uncertain for "
                        f"{rule.for_seconds:.0f}s", rows[-1][0], rows[-1][1])
            return False, "", None, None
        compare = COMPARISONS.get(rule.condition)
        if compare is None or rule.threshold is None:
            return False, "", None, None
        # Only GOOD samples can trip a threshold. A Bad sample carries no value,
        # and a threshold rule that fires on one is reporting a number that does
        # not exist.
        good = [(v, q) for v, q, _ in rows if q == 0 and v is not None]
        if not good or len(good) < len(rows):
            return False, "", None, None
        if all(compare(v, rule.threshold) for v, _ in good):
            return (True, f"{rule.subject} {rule.condition} {rule.threshold:g} "
                    f"for {rule.for_seconds:.0f}s (now {good[-1][0]:.4g})",
                    good[-1][0], 0)
        return False, "", good[-1][0], 0

    if rule.subject_kind == "kpi":
        # "GrossUnitHeatRate@KPCL-RTPS-U1": a KPI on one element. A KPI is
        # computed for every element it applies to, so a rule naming only the
        # KPI would follow whichever element happened to be computed last.
        name, _, asset_code = rule.subject.partition("@")
        sql = ("SELECT k.value, k.quality, k.reason FROM kpi_value k"
               " JOIN kpi_definition d ON d.id = k.kpi_definition_id"
               " JOIN element e ON e.id = k.element_id WHERE d.name = %s")
        params: tuple = (name,)
        if asset_code:
            sql += " AND e.asset_code = %s"
            params += (asset_code,)
        with conn.cursor() as cur:
            cur.execute(sql + " ORDER BY k.ts DESC LIMIT 1", params)
            row = cur.fetchone()
        if row is None:
            return False, "", None, None
        value, quality, reason = row
        if rule.condition == "bad" and _severity(quality) == 2:
            return True, f"{rule.subject} is Bad: {reason}", None, quality
        if rule.condition in COMPARISONS and value is not None and quality == 0:
            if COMPARISONS[rule.condition](value, rule.threshold):
                return (True, f"{rule.subject} {rule.condition} "
                        f"{rule.threshold:g} (now {value:.4g})", value, quality)
        return False, "", value, quality

    if rule.subject_kind == "capacity":
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM capacity_sample WHERE metric = %s"
                        " ORDER BY ts DESC LIMIT 1", (rule.subject,))
            row = cur.fetchone()
        if row is None or rule.threshold is None:
            return False, "", None, None
        compare = COMPARISONS.get(rule.condition)
        if compare and compare(row[0], rule.threshold):
            return (True, f"capacity {rule.subject} {rule.condition} "
                    f"{rule.threshold:g} (now {row[0]:,.0f})", row[0], 0)
        return False, "", row[0], 0

    if rule.subject_kind == "collector":
        with conn.cursor() as cur:
            cur.execute("SELECT alive, link_up, buffer_depth FROM collector_leader"
                        " WHERE instance = %s", (rule.subject,))
            row = cur.fetchone()
        if row is None:
            return True, f"collector {rule.subject} has never reported", None, None
        alive, link_up, depth = row
        if not alive:
            return True, f"collector {rule.subject} is not reporting", None, None
        if rule.condition == ">" and rule.threshold is not None and depth > rule.threshold:
            return (True, f"collector {rule.subject} buffer depth {depth} "
                    f"> {rule.threshold:g}", float(depth), 0)
        return False, "", float(depth), 0

    return False, "", None, None


def raise_or_clear(conn: psycopg.Connection, rule: Rule, firing: bool,
                   detail: str, value, quality) -> str | None:
    """Open an alert, or close the open one. Returns 'raised', 'cleared', or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM alert WHERE rule_id = %s AND cleared_at IS NULL",
                    (rule.id,))
        open_alert = cur.fetchone()
        if firing and open_alert is None:
            cur.execute(
                "INSERT INTO alert (rule_id, severity, detail, value, quality)"
                " VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (rule.id, rule.severity, detail, value, quality))
            conn.commit()
            return "raised"
        if not firing and open_alert is not None:
            cur.execute("UPDATE alert SET cleared_at = now() WHERE id = %s",
                        (open_alert[0],))
            conn.commit()
            return "cleared"
    return None


def deliver(conn: psycopg.Connection, alert_id: int, rule: Rule,
            detail: str) -> None:
    """Email, if configured. A failure is recorded, never swallowed."""
    host = os.environ.get("CRPMS_SMTP_HOST")
    if not host or not rule.recipients:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alert SET delivery_error = %s WHERE id = %s",
                ("no SMTP host configured" if not host else "no recipients",
                 alert_id))
        conn.commit()
        return
    message = EmailMessage()
    message["Subject"] = f"[CRPMS {rule.severity}] {rule.name}"
    message["From"] = os.environ.get("CRPMS_SMTP_FROM", "crpms@localhost")
    message["To"] = ", ".join(rule.recipients)
    message.set_content(
        f"{rule.name}\n\n{detail}\n\n"
        f"Severity: {rule.severity}\n"
        f"Raised:   {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
    try:
        port = int(os.environ.get("CRPMS_SMTP_PORT", "25"))
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if os.environ.get("CRPMS_SMTP_STARTTLS", "0") == "1":
                smtp.starttls()
            user = os.environ.get("CRPMS_SMTP_USER")
            if user:
                smtp.login(user, os.environ.get("CRPMS_SMTP_PASSWORD", ""))
            smtp.send_message(message)
        with conn.cursor() as cur:
            cur.execute("UPDATE alert SET delivered = true WHERE id = %s",
                        (alert_id,))
    except Exception as exc:
        with conn.cursor() as cur:
            cur.execute("UPDATE alert SET delivery_error = %s WHERE id = %s",
                        (str(exc)[:400], alert_id))
        log.error("alert %d not delivered: %s", alert_id, exc)
    conn.commit()


def run_once(conn: psycopg.Connection) -> dict:
    counts = {"raised": 0, "cleared": 0, "checked": 0}
    for rule in load_rules(conn):
        counts["checked"] += 1
        firing, detail, value, quality = evaluate(conn, rule)
        outcome = raise_or_clear(conn, rule, firing, detail, value, quality)
        if outcome == "raised":
            counts["raised"] += 1
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM alert WHERE rule_id = %s"
                            " ORDER BY raised_at DESC LIMIT 1", (rule.id,))
                alert_id = cur.fetchone()[0]
            deliver(conn, alert_id, rule, detail)
        elif outcome == "cleared":
            counts["cleared"] += 1
    return counts


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ops.alerts")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    import time
    logging.Formatter.converter = time.gmtime      # UTC, like every other log
    while True:
        with psycopg.connect(DSN) as conn:
            counts = run_once(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT r.name, a.severity, a.detail, a.raised_at"
                            " FROM alert a JOIN alert_rule r ON r.id = a.rule_id"
                            " WHERE a.cleared_at IS NULL ORDER BY a.raised_at")
                open_alerts = cur.fetchall()
        log.info("checked %d rules: %d raised, %d cleared, %d open",
                 counts["checked"], counts["raised"], counts["cleared"],
                 len(open_alerts))
        for name, severity, detail, raised in open_alerts:
            log.warning("OPEN [%s] %s — %s (since %s UTC)", severity, name, detail,
                        raised.astimezone(dt.timezone.utc).strftime("%H:%M:%S"))
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
