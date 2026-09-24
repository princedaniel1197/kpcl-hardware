"""The FAT runner: execute what can be automated, capture evidence, report.

Produces a markdown report with one row per test, the measured result, the
evidence behind it, and signature blocks. Tests that cannot honestly be
automated appear as hold or witness points with space for a signature, not as
green ticks.

The report states the environment it was produced in, because a performance
figure without the machine it was measured on is decoration.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import subprocess
import sys
from pathlib import Path

import psycopg

from fat import tests as fat_tests
from fat.plan import PLAN
from fat.tests import AUTOMATED, HOLD, WITNESS, Result
from ops import access

ROOT = Path(__file__).parent.parent
DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")


def git_state() -> tuple[str, str]:
    """The commit, and anything in the working tree that differs from it."""
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            cwd=str(ROOT), capture_output=True,
                            text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                           capture_output=True, text=True).stdout.strip()
    return commit, dirty


def environment(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        pg = cur.fetchone()[0].split(" on ")[0]
        cur.execute("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")
        ts = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sample")
        samples = cur.fetchone()[0]
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
        size = cur.fetchone()[0]
    commit, dirty = git_state()
    return {
        "host": platform.platform(),
        "python": sys.version.split()[0],
        "postgres": pg,
        "timescaledb": ts,
        "samples": f"{samples:,}",
        "database_size": size,
        "commit": commit + (" (working tree not clean)" if dirty else ""),
    }


def run(selected: set[str] | None = None, skip: set[str] | None = None,
        allow_dirty: bool = False) -> Path:
    # The report names the commit it tested. On a dirty tree that commit is not
    # the code that ran, and the report would say something untrue about
    # itself. The acceptance report of 22 September was produced that way.
    commit, dirty = git_state()
    if dirty and not allow_dirty:
        raise SystemExit(
            "the working tree has changes not in the commit, so the report "
            f"could not say what code it tested:\n{dirty}\n"
            "commit them, or pass --allow-dirty to produce a report marked "
            "NOT FOR ACCEPTANCE")

    began = dt.datetime.now(dt.timezone.utc)
    results: dict[str, Result] = {}

    with psycopg.connect(DSN, autocommit=True) as conn:
        env = environment(conn)
        # A principal for this run, least privilege (corporate: read only),
        # revoked when the run ends. Created and revoked through the audited
        # path like any other.
        username = f"fat.runner.{began.strftime('%Y%m%dT%H%M%SZ')}"
        fat_tests.API_TOKEN = access.create_principal(
            conn, username, "corporate", actor="fat.runner")
        try:
            for test in PLAN:
                if selected and test.ref not in selected:
                    continue
                if skip and test.ref in skip:
                    continue
                if test.kind != AUTOMATED or test.run is None:
                    continue
                print(f"  {test.ref}  {test.title} ... ", end="", flush=True)
                try:
                    result = test.run(conn)
                except Exception as exc:                      # noqa: BLE001
                    result = Result(False, f"error: {exc}")
                results[test.ref] = result
                print("PASS" if result.passed else
                      "NOT RUN" if result.passed is None else "FAIL")
        finally:
            access.revoke(conn, username, actor="fat.runner")
            fat_tests.API_TOKEN = None

    ended = dt.datetime.now(dt.timezone.utc)
    report = render(env, results, began, ended, dirty=bool(dirty))
    out = ROOT / "fat" / "reports" / f"FAT-{began.strftime('%Y%m%dT%H%M%SZ')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    return out


def render(env: dict, results: dict[str, Result], began, ended,
           dirty: bool = False) -> str:
    automated = [t for t in PLAN if t.kind == AUTOMATED and t.run is not None]
    ran = [t for t in automated if t.ref in results]
    passed = [t for t in ran if results[t.ref].passed is True]
    failed = [t for t in ran if results[t.ref].passed is False]
    not_run = [t for t in ran if results[t.ref].passed is None]
    manual = [t for t in PLAN if t.kind in (HOLD, WITNESS)]
    covered = [t for t in PLAN if t.kind == AUTOMATED and t.run is None]

    lines = [
        "# Factory Acceptance Test — CRPMS Demonstrator",
        "",
        "Orianode Technologies, for the Karnataka Power Corporation Limited",
        "Centralised Real-Time Performance Monitoring and Asset Management System.",
        "",
        f"**Report generated {began.isoformat(timespec='seconds')}**, "
        f"execution took {(ended - began).total_seconds():.0f} s.",
        "",
        *(["> **NOT FOR ACCEPTANCE.** Produced from a working tree with changes "
            "not in the commit named below, so this report cannot say exactly "
            "what code it tested.", ""] if dirty else []),
        "## Result",
        "",
        f"| | |", "|---|---|",
        f"| Automated tests executed | {len(ran)} |",
        f"| Passed | **{len(passed)}** |",
        f"| Failed | **{len(failed)}** |",
        f"| Not run | {len(not_run)} |",
        f"| Hold and witness points outstanding | {len(manual)} |",
        "",
    ]
    if failed:
        lines += ["**This report contains failures.** They are listed in full "
                  "below and are not summarised away.", ""]
    lines += [
        "This report is generated from measurements taken at the time it was "
        "produced. No figure in it is carried over from a previous run.",
        "",
        "## Environment",
        "", "| | |", "|---|---|",
    ]
    for key, value in env.items():
        lines.append(f"| {key.replace('_', ' ').title()} | {value} |")

    lines += ["", "## Automated tests", "",
              "| Ref | Clause | Test | Criterion | Actual | Result |",
              "|---|---|---|---|---|---|"]
    for test in ran:
        r = results[test.ref]
        verdict = "**PASS**" if r.passed else ("not run" if r.passed is None
                                                else "**FAIL**")
        lines.append(f"| {test.ref} | {test.clause} | {test.title} | "
                     f"{test.criterion} | {r.actual} | {verdict} |")

    lines += ["", "## Evidence", ""]
    for test in ran:
        r = results[test.ref]
        lines += [f"### {test.ref} — {test.title} ({test.clause})", "",
                  f"- **Condition:** {test.condition}",
                  f"- **Method:** {test.method}",
                  f"- **Criterion:** {test.criterion}",
                  f"- **Would fail if:** {test.fails_if or 'none stated'}",
                  f"- **Actual:** {r.actual}",
                  f"- **Result:** "
                  + ("PASS" if r.passed else "NOT RUN" if r.passed is None
                     else "FAIL")]
        for item in r.evidence:
            lines.append(f"  - {item}")
        if r.detail:
            lines += ["", f"  > {r.detail}"]
        if test.note:
            lines += ["", f"  *{test.note}*"]
        lines.append("")

    if covered:
        lines += ["## Covered by the automated suite", "",
                  "| Ref | Clause | Test | Where |", "|---|---|---|---|"]
        for test in covered:
            lines.append(f"| {test.ref} | {test.clause} | {test.title} | "
                         f"{test.note} |")
        lines.append("")

    lines += [
        "## Hold and witness points",
        "",
        "These are not automated, and deliberately so. A hold point stops the "
        "FAT until it is signed off; a witness point must be observed by a "
        "person. Automating either into a green tick would make the report "
        "shorter and worth less.",
        "",
        "| Ref | Clause | Type | Test | Criterion | Result | Signature | Date |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for test in manual:
        lines.append(f"| {test.ref} | {test.clause} | {test.kind} | "
                     f"{test.title} | {test.criterion} | | | |")
    lines += [""]
    for test in manual:
        lines += [f"**{test.ref} — {test.title}**  ",
                  f"Condition: {test.condition}  ",
                  f"Method: {test.method}  ",
                  f"{test.note}", ""]

    lines += [
        "## What this demonstrator does not claim",
        "",
        "Stated here rather than in a footnote, because the second list is what "
        "makes the first believable.",
        "",
        "It does **not** touch a real BHEL, Yokogawa, ABB or Andritz DCS. It is "
        "**not** running on a licensed AVEVA PI installation, and whether a real "
        "PI Web API endpoint accepts its OMF has not been tested. It is **not** "
        "proven at 34,700 I/O across 13 sites; every figure here was measured on "
        "one laptop with one simulated unit and a bench rig. It says nothing "
        "about wide-area network behaviour, cross-site time synchronisation, "
        "per-station licensing, or OT security zoning. The bench rig passed its "
        "probe-failure test on real hardware, but its current reading is "
        "uncalibrated -- published as Uncertain and used by no KPI or alert -- "
        "and its relays and run switch are not fitted.",
        "",
        "Plant physics is limited to definitional ratios. Cylinder efficiency "
        "and condenser performance require published steam tables and are not "
        "implemented, rather than approximated.",
        "",
        "## Sign-off",
        "",
        "| Role | Name | Signature | Date |",
        "|---|---|---|---|",
        "| Performed by | | | |",
        "| Witnessed by | | | |",
        "| Accepted for Orianode Technologies | | | |",
        "| Accepted for KPCL | | | |",
        "",
        f"Report produced by `fat/runner.py` at "
        f"{ended.isoformat(timespec='seconds')}.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(prog="fat.runner")
    ap.add_argument("--only", help="comma-separated refs, e.g. T-01,T-07")
    ap.add_argument("--skip", help="comma-separated refs to skip")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run on a working tree with uncommitted changes; the "
                         "report is marked NOT FOR ACCEPTANCE")
    args = ap.parse_args()
    selected = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else None
    print("Running the automated FAT tests\n")
    out = run(selected, skip, allow_dirty=args.allow_dirty)
    print(f"\nreport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
