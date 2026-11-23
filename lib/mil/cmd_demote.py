"""Step an enforced check down one level (e.g. block -> comment on a false positive)."""

from __future__ import annotations

import argparse
from typing import Any

from mil.errors import MilError, mil_line
from mil.human_ok import approver_looks_like_agent
from mil.ledger import find_by_id, save_record_file, today
from mil.models import ROLLOUT
from mil.paths import mil_dir

DEMOTABLE = ("block", "comment", "shadow")
TARGETS = ("comment", "shadow", "proven")

DEMOTE_EPILOG = """\
Step a check down one enforcement level when a false positive or noisy
edge case appears, while keeping the check and its fixtures intact:

  block   -> comment
  comment -> shadow
  shadow  -> proven

usage:
  mil demote F-20260609-01 --reason "False positive on docs-only hotfix branch" --by @owner
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "demote",
        help="step an enforced record down one level (block -> comment -> shadow -> proven)",
        description="Step an enforced record down one level (block -> comment -> shadow -> proven).",
        epilog=DEMOTE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("incident", metavar="F-YYYYMMDD-NN", help="ledger record id")
    p.add_argument("--to", choices=TARGETS, help="target state (must be one step below current)")
    p.add_argument("--reason", required=True, help="why the check is being stepped down")
    p.add_argument("--by", help="who requested the demotion, e.g. @owner")
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
    if args.by and approver_looks_like_agent(args.by):
        raise MilError(
            mil_line(rec_path, f"--by {args.by!r} names an agent", "demotion approvals must name a person")
        )

    current = (rec.get("enforcement") or {}).get("state")
    if current not in DEMOTABLE:
        raise MilError(
            mil_line(
                rec_path,
                f"cannot demote from {current!r}",
                "only block, comment, and shadow can be stepped down",
            )
        )
    prev_state = ROLLOUT[ROLLOUT.index(current) - 1]
    target = args.to or prev_state
    if target != prev_state:
        raise MilError(
            mil_line(
                rec_path,
                f"{current} -> {target} skips a state",
                f"demote one step at a time; the next step down is --to {prev_state}",
            )
        )

    rid = rec.get("id", args.incident)
    note = f"mil demote: {reason}"
    if args.dry_run:
        print(f"would demote {rid} {current} -> {target} ({note})")
        return 0
    _apply(rec, target, args.by, note)
    save_record_file(rec_path, rec)
    print(f"demoted {rid} {current} -> {target} -> {rec_path}")
    return 0


def _apply(rec: dict[str, Any], target: str, by: str | None, note: str) -> None:
    rec.setdefault("enforcement", {})["state"] = target
    hist = rec.setdefault("history", [])
    entry: dict[str, Any] = {"state": target, "at": today(), "note": note}
    if by and by.strip():
        entry["by"] = by.strip()
    hist.append(entry)
