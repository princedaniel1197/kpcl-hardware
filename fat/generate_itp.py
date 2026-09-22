"""Render the Inspection and Test Plan from fat/plan.py.

Generated, never hand-written, so the plan a reviewer reads and the plan the
runner executes cannot drift apart.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fat.plan import PLAN
from fat.tests import AUTOMATED, HOLD, WITNESS


def render() -> str:
    lines = [
        "# Inspection and Test Plan",
        "",
        "Orianode Technologies · CRPMS Demonstrator · for KPCL",
        "",
        "**Generated from `fat/plan.py`.** Do not edit by hand: the runner "
        "executes this same list, and a hand-edited copy would describe tests "
        "that are not the ones being run.",
        "",
        f"Generated {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}.",
        "",
        "## Summary",
        "",
        f"| | |", "|---|---|",
        f"| Tests | {len(PLAN)} |",
        f"| Automated | {sum(1 for t in PLAN if t.kind == AUTOMATED)} |",
        f"| Hold points | {sum(1 for t in PLAN if t.kind == HOLD)} |",
        f"| Witness points | {sum(1 for t in PLAN if t.kind == WITNESS)} |",
        "",
        "A hold point stops the FAT until it is signed off. A witness point "
        "must be observed by a person. Neither is automated, deliberately: "
        "turning either into a green tick would make the report shorter and "
        "worth less.",
        "",
        "## The plan",
        "",
        "| Ref | Clause | Test | Condition | Method | Acceptance criterion | Type | Actual | Result | Signature |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in PLAN:
        lines.append(
            f"| {t.ref} | {t.clause} | {t.title} | {t.condition} | {t.method} | "
            f"**{t.criterion}** | {t.kind} | | | |")
    lines += ["", "## Notes", ""]
    for t in PLAN:
        if t.note:
            lines.append(f"- **{t.ref}** — {t.note}")
    lines += [
        "",
        "## Clause coverage",
        "",
        "| Clause | Tests |",
        "|---|---|",
    ]
    clauses: dict[str, list[str]] = {}
    for t in PLAN:
        for clause in [c.strip() for c in t.clause.split(",")]:
            clauses.setdefault(clause, []).append(t.ref)
    def clause_order(clause: str) -> int:
        # Clauses can be ranges ("§512–518"), so sort on the first number.
        digits = ""
        for ch in clause.lstrip("§"):
            if ch.isdigit():
                digits += ch
            else:
                break
        return int(digits) if digits else 0

    for clause in sorted(clauses, key=clause_order):
        lines.append(f"| {clause} | {', '.join(clauses[clause])} |")
    lines += [
        "",
        "## Sign-off",
        "",
        "| Role | Name | Signature | Date |",
        "|---|---|---|---|",
        "| Prepared by | | | |",
        "| Reviewed by | | | |",
        "| Accepted for KPCL | | | |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    out = Path(__file__).parent / "ITP.md"
    out.write_text(render())
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
