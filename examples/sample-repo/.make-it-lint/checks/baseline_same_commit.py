#!/usr/bin/env python3
# ---
# id: MIL-BASELINE-001
# incident: F-20260402-01
# input: diff
# layer: test
# tool: script
# ---
"""Fail when a test file and its stored baseline change in the same diff.

Updating the expected output in the same commit as the test hides a
regression: the suite stays green while the behaviour drifts.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _support import (  # noqa: E402
    changed_paths,
    check_config,
    die,
    emit,
    parse_diff_args,
    path_in_prefixes,
)

CHECK_ID = "MIL-BASELINE-001"


def main(argv: list[str] | None = None) -> int:
    args = parse_diff_args(argv)
    cfg = check_config(Path(args.config), CHECK_ID)
    baseline_paths = cfg.get("baseline_paths") or []
    test_paths = cfg.get("test_paths") or []
    if not baseline_paths or not test_paths:
        die(f"checks.{CHECK_ID} needs baseline_paths and test_paths")
    changed = changed_paths(Path(args.base), Path(args.head))
    baselines = [p for p in changed if path_in_prefixes(p, baseline_paths)]
    tests = [
        p
        for p in changed
        if path_in_prefixes(p, test_paths) and not path_in_prefixes(p, baseline_paths)
    ]
    if not baselines or not tests:
        return 0
    message = cfg.get("message") or (
        "test code and stored baselines changed together; "
        "split the baseline update so a regression cannot hide in the expected output"
    )
    for path in tests + baselines:
        emit(path, message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
