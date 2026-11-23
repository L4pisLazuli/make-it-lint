"""Summarize ledger records by enforcement state."""

from __future__ import annotations

import argparse

from mil.errors import MilError, mil_line
from mil.ledger import iter_records
from mil.models import STATES
from mil.paths import mil_dir


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "status",
        help="count and list records by state",
        epilog="examples:\n  mil status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = args.root
    if not mil_dir(root).is_dir():
        raise MilError(mil_line(root, "no .make-it-lint directory"))
    buckets: dict[str, list[tuple[str, str]]] = {state: [] for state in STATES}
    unknown: list[tuple[str, str, str]] = []
    for path, rec in iter_records(root):
        incident_id = str(rec.get("id") or path.parent.name)
        title = str(rec.get("title") or "")
        state = (rec.get("enforcement") or {}).get("state")
        if state in buckets:
            buckets[state].append((incident_id, title))
        else:
            unknown.append((incident_id, str(state), title))
    width = max(len(s) for s in STATES)
    total = 0
    for state in STATES:
        n = len(buckets[state])
        total += n
        print(f"{state:<{width}}  {n}")
    print(f"{'total':<{width}}  {total}")
    for state in STATES:
        rows = buckets[state]
        if not rows:
            continue
        print()
        print(f"{state}:")
        for incident_id, title in rows:
            print(f"  {incident_id}  {title}")
    if unknown:
        print()
        print("unrecognized state:")
        for incident_id, state, title in unknown:
            print(f"  {incident_id}  {state}  {title}")
        return 1
    return 0
