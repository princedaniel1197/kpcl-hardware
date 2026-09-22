"""Stage 7 acceptance test.

From the build plan: force U1_COAL_FLOW to Bad through the Stage 1 control API.
Heat rate goes Bad within one calculation cycle, naming coal flow as the cause.
It does not go to zero.

Run against the live simulator, collector and archive.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

import psycopg

from engine import kpi

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
CONTROL = os.environ.get("SIM_CONTROL", "http://127.0.0.1:8081")
CLASS = ("Good", "Uncertain", "Bad", "Reserved")


def control(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body else None
    request = urllib.request.Request(
        CONTROL + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=5) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def cycle(conn, element_id, heading):
    print(f"\n  {heading}")
    results = {}
    for d in kpi.load_definitions(conn):
        r = kpi.compute(conn, d, element_id)
        kpi.store(conn, r)
        results[d.name] = r
        value = f"{r.value:11.2f}" if r.value is not None else "  (no value)"
        print(f"    {d.name:<27} v{d.version} {value} "
              f"{d.engineering_unit or '':<9} {CLASS[kpi.severity(r.quality)]}")
        if r.reason:
            print(f"        reason: {r.reason}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", type=float, default=8.0)
    args = ap.parse_args()
    checks: dict[str, bool] = {}

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM element WHERE asset_code='KPCL-RTPS-U1'")
            element_id = cur.fetchone()[0]

        print("=" * 76)
        print("STAGE 7 — a KPI with a Bad input returns Bad, naming the input")
        print("=" * 76)

        control("DELETE", "/quality")
        time.sleep(args.settle)
        before = cycle(conn, element_id, "BEFORE — all inputs Good")
        hr = before["GrossUnitHeatRate"]
        checks["heat rate is Good beforehand"] = hr.is_good
        checks["heat rate is a plausible number"] = (
            hr.value is not None and 1800 < hr.value < 5000)

        print(f"\n  forcing U1_COAL_FLOW to BadDeviceFailure ...")
        control("POST", "/quality/U1_COAL_FLOW", {"quality": "BadDeviceFailure"})
        time.sleep(args.settle)

        after = cycle(conn, element_id, "AFTER — one calculation cycle later")
        hr_bad = after["GrossUnitHeatRate"]
        sc_bad = after["SpecificCoalConsumption"]
        aux = after["AuxiliaryPowerConsumption"]

        checks["heat rate went Bad"] = kpi.severity(hr_bad.quality) == 2
        checks["heat rate names coal flow"] = "CoalFlow" in (hr_bad.reason or "")
        checks["heat rate did NOT go to zero"] = hr_bad.value is None
        checks["specific coal also went Bad"] = kpi.severity(sc_bad.quality) == 2
        checks["aux power, which does not use coal, stayed Good"] = aux.is_good

        # It is visible downstream, with the version that produced it.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kpi_name, kpi_version, value, quality_class, reason"
                " FROM kpi_value_decoded WHERE kpi_name = 'GrossUnitHeatRate'"
                " ORDER BY ts DESC LIMIT 2")
            rows = cur.fetchall()
        print("\n  stored in the archive (kpi_value_decoded):")
        for name, version, value, klass, reason in rows:
            print(f"    v{version}  {str(value):>10}  {klass:<10} {reason or ''}")
        checks["stored Bad with no value and a reason"] = bool(
            rows and rows[0][2] is None and rows[0][3] == "Bad" and rows[0][4])
        checks["stored with the definition version"] = bool(rows and rows[0][1] == 1)

        control("DELETE", "/quality/U1_COAL_FLOW")
        time.sleep(args.settle)
        restored = cycle(conn, element_id, "RESTORED — coal flow Good again")
        checks["heat rate recovered"] = restored["GrossUnitHeatRate"].is_good

        print("\n" + "=" * 76)
        for name, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed = [n for n, ok in checks.items() if not ok]
        print("=" * 76)
        print(f"STAGE 7: {'PASS' if not failed else 'FAIL — ' + ', '.join(failed)}")
        return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
