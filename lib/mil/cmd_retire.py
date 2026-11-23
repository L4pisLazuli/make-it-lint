"""Retire a ledger record once its structural root cause is gone."""

from __future__ import annotations

import argparse
from typing import Any

from mil.errors import MilError, mil_line
from mil.ledger import find_by_id, save_record_file, today
from mil.paths import mil_dir

RETIRE_EPILOG = """\
Retire a ledger record when the failure is structurally impossible (for
example after a type/architecture change) or the affected subsystem was
removed. Retired checks stop running in CI while keeping their record
and fixtures in the ledger.

usage:
  mil retire F-20260609-01 --reason "Catalog is now generated at build time and removed from git" --by @owner
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "retire",
        help="retire a record after its structural cause is eliminated",
        description="Retire a record after its structural cause is eliminated.",
        epilog=RETIRE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("incident", metavar="F-YYYYMMDD-NN", help="ledger record id")
    p.add_argument("--reason", required=True, help="why this failure can no longer happen")
    p.add_argument("--by", help="who approved retiring the record, e.g. @owner")
    p.add_argument("--dry-run", action="store_true", help="print the step and write nothing")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = args.root
    if not mil_dir(root).is_dir():
        raise MilError(mil_line(root, "no .make-it-lint directory"))
    found = find_by_id(root, args.incident)
    if found is None:
        raise MilError(mil_line(args.incident, "no such ledger record"))
    rec_path, rec = found
    reason = args.reason.strip()
    if not reason:
        raise MilError(mil_line(rec_path, "--reason is empty"))
    if args.by and "agent" in args.by.lower():
        raise MilError(
            mil_line(rec_path, f"--by {args.by!r} names an agent", "retirement requires a human decision")
        )

    current = (rec.get("enforcement") or {}).get("state")
    if current == "retired":
        raise MilError(mil_line(rec_path, "record is already retired"))

    rid = rec.get("id", args.incident)
    note = f"mil retire: {reason}"
    if args.dry_run:
        print(f"would retire {rid} {current} -> retired ({note})")
        return 0
    _apply(rec, args.by, note)
    save_record_file(rec_path, rec)
    print(f"retired {rid} {current} -> retired -> {rec_path}")
    return 0


def _apply(rec: dict[str, Any], by: str | None, note: str) -> None:
    rec.setdefault("enforcement", {})["state"] = "retired"
    hist = rec.setdefault("history", [])
    entry: dict[str, Any] = {"state": "retired", "at": today(), "note": note}
    if by and by.strip():
        entry["by"] = by.strip()
    hist.append(entry)
