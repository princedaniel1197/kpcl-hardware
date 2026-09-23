"""Stage 9 acceptance test.

From the build plan: messages validate against the OMF schema. Changing one
config value switches the destination.

Runs the collector's OMF path end to end against the receiver, then shows that
one environment variable moves the destination.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

from archive import audit
from collector import omf
from collector.model import Sample

ROOT = Path(__file__).parent.parent
DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
PORT_A = 8082
PORT_B = 8083
DEMO_TAG = "OMF_DEMO"


def start_receiver(port: int) -> subprocess.Popen:
    """Start a receiver of our own on `port`. If something already answers
    there, refuse: the demonstration would be talking to a process it did not
    start, running whatever code that process was started with. (Found on 23
    September: a receiver left over from the day before answered, and its
    counts were reported as this run's.)"""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/omf/status", timeout=2)
        raise RuntimeError(f"something is already serving port {port}; stop it "
                           "first so this run talks to a receiver it started")
    except urllib.error.URLError:
        pass
    p = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "archive.omf_receiver",
         "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ})
    for _ in range(40):
        if p.poll() is not None:
            raise RuntimeError(f"the receiver on {port} exited ({p.returncode})")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/omf/status",
                                   timeout=2).read()
            return p
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"receiver on {port} did not start")


def status(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/omf/status",
                                timeout=5) as r:
        return json.loads(r.read())


def main() -> int:
    checks: dict[str, bool] = {}
    receivers = []
    try:
        print("=" * 76)
        print("STAGE 9 — OMF: messages validate, one config value moves them")
        print("=" * 76)

        receivers = [start_receiver(PORT_A), start_receiver(PORT_B)]
        print(f"\n  two OMF receivers running on {PORT_A} and {PORT_B}")

        # The demonstration's synthetic samples go under a tag of their own.
        # The first version wrote them under U1_AUX_POWER -- a 123.45 MW
        # auxiliary load and a Bad reading, in a real tag's history, where the
        # trend, the export and the KPI engine would all take them for plant
        # data. Found and removed on 23 September.
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM tag WHERE name = %s", (DEMO_TAG,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO tag (name, description, engineering_unit,"
                    " source_system) VALUES (%s, %s, 'MW', 'omf-demo') RETURNING id",
                    (DEMO_TAG, "Stage 9 demonstration: synthetic values sent "
                               "through OMF. Not plant data."))
                audit.record(cur, actor="omf-demo", entity="tag",
                             entity_id=cur.fetchone()[0], field=None, old=None,
                             new=DEMO_TAG, reason="tag for the Stage 9 demonstration")
                conn.commit()
            cur.execute("SELECT id, name, description, engineering_unit FROM tag"
                        " WHERE name = %s", (DEMO_TAG,))
            tags = [{"id": r[0], "name": r[1], "description": r[2],
                     "engineering_unit": r[3]} for r in cur.fetchall()]

        # --- validation -------------------------------------------------
        print("\n  1. THE THREE MESSAGE KINDS")
        type_msg = omf.type_message()
        container_msg = omf.container_message(tags)
        now = dt.datetime.now(dt.timezone.utc)
        samples = [
            Sample(tags[0]["id"], tags[0]["name"], now, now + dt.timedelta(milliseconds=42),
                   123.45, 0, 1),
            # A Bad sample, carrying no value: the case OMF would default to 0.
            Sample(tags[0]["id"], tags[0]["name"], now + dt.timedelta(seconds=1),
                   now + dt.timedelta(seconds=1, milliseconds=42), None,
                   2156593152, 2),
        ]
        data_msg = omf.data_message(samples)
        for kind, body in (("type", type_msg), ("container", container_msg),
                           ("data", data_msg)):
            omf.validate(kind, "create", body)
            print(f"     {kind:<10} {len(body)} object(s)  VALIDATES")
        checks["all three message kinds validate"] = True

        print(f"\n     index property : "
              f"{[n for n, p in type_msg[0]['properties'].items() if p.get('isindex')]}")
        print(f"     quality property: "
              f"{[n for n, p in type_msg[0]['properties'].items() if p.get('isquality')]}")
        checks["the index is SourceTime"] = (
            type_msg[0]["properties"]["SourceTime"].get("isindex") is True)
        checks["quality is a designated OMF quality property"] = (
            type_msg[0]["properties"]["Quality"].get("isquality") is True)

        # --- send to destination A --------------------------------------
        print(f"\n  2. SENDING TO DESTINATION A (port {PORT_A})")
        config_a = omf.OmfConfig(url=f"http://127.0.0.1:{PORT_A}/omf")
        client = omf.OmfClient(config_a)
        for kind, body in (("type", type_msg), ("container", container_msg),
                           ("data", data_msg)):
            code = client.post(kind, "create", body)
            print(f"     POST {kind:<10} -> HTTP {code}")
        a = status(PORT_A)
        print(f"     receiver A: {a['counts']}")
        checks["destination A received the values"] = a["counts"]["values"] == 2

        # --- the Bad sample landed as NULL, not zero ---------------------
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value, quality FROM sample WHERE tag_id=%s"
                " AND source_ts >= %s ORDER BY source_ts", (tags[0]["id"], now))
            rows = cur.fetchall()
        print(f"\n     stored: {rows}")
        checks["the Good sample stored its value"] = (
            bool(rows) and rows[0][0] is not None)
        checks["the Bad sample stored NULL, not zero"] = (
            len(rows) > 1 and rows[1][0] is None and rows[1][1] == 2156593152)

        # --- omitting Value is refused ----------------------------------
        print("\n  3. A VALUE OMITTED IS REFUSED")
        print("     (OMF would default an omitted number to 0 — that is the")
        print("      substitution §318 forbids, arriving through the wire format)")
        bad_body = [{"containerid": tags[0]["name"],
                     "values": [{"SourceTime": omf._iso(now + dt.timedelta(seconds=5)),
                                 "Quality": 2156593152}]}]
        try:
            client.post("data", "create", bad_body)
            refused = False
        except omf.OmfError as exc:
            refused = True
            print(f"     refused: {str(exc)[:120]}")
        checks["omitting Value is refused"] = refused

        # --- switch destination with one config value --------------------
        print(f"\n  4. CHANGING ONE CONFIG VALUE (port {PORT_A} -> {PORT_B})")
        config_b = omf.OmfConfig(url=f"http://127.0.0.1:{PORT_B}/omf")
        client_b = omf.OmfClient(config_b)
        for kind, body in (("type", type_msg), ("container", container_msg),
                           ("data", data_msg)):
            client_b.post(kind, "create", body)
        b = status(PORT_B)
        a_after = status(PORT_A)
        print(f"     receiver A: {a_after['counts']['data']} data messages "
              f"(unchanged)")
        print(f"     receiver B: {b['counts']['data']} data messages")
        checks["the destination moved"] = (
            b["counts"]["data"] >= 1 and a_after["counts"]["data"] == a["counts"]["data"])
        checks["only the URL differed"] = (
            config_a.producer_token == config_b.producer_token
            and config_a.url != config_b.url)

        print("\n" + "=" * 76)
        for name, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed = [n for n, ok in checks.items() if not ok]
        print("=" * 76)
        print(f"STAGE 9: {'PASS' if not failed else 'FAIL — ' + ', '.join(failed)}")
        return 0 if not failed else 1
    finally:
        for p in receivers:
            p.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
