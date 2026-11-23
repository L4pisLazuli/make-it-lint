#!/usr/bin/env python3
# ---
# id: MIL-GEN-001
# incident: F-20260609-01
# input: diff
# layer: ci
# tool: script
# ---
"""Fail when a pull request edits a generated file.

Two branches regenerating the same index conflict on merge. The source
of the index belongs in the PR; regenerate the derived file after merge.
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

CHECK_ID = "MIL-GEN-001"


def main(argv: list[str] | None = None) -> int:
    args = parse_diff_args(argv)
    cfg = check_config(Path(args.config), CHECK_ID)
    generated = cfg.get("generated_paths") or []
    if not generated:
        die(f"checks.{CHECK_ID}.generated_paths is missing")
    message = cfg.get("message") or (
        "do not change generated files in a pull request; regenerate them after merge"
    )
    hits = 0
    for path in changed_paths(Path(args.base), Path(args.head)):
        if path_in_prefixes(path, generated):
            emit(path, message)
            hits += 1
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
