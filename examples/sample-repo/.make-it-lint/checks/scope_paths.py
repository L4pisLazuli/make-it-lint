#!/usr/bin/env python3
# ---
# id: MIL-SCOPE-001
# incident: F-20260311-01
# input: diff
# layer: ci
# tool: script
# ---
"""Fail when a restricted PR kind changes a path outside its allowlist.

A single-file linter never sees the PR kind and the rest of the diff at
once, so this check diffs base/head and reads the kind from pr.yaml or
MIL_PR_KIND / MIL_PR_LABELS / MIL_PR_BRANCH. Pull requests whose branch
or labels are not one of the restricted kinds pass with exit 0.
"""

from __future__ import annotations

import os
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
    load_yaml_path,
    parse_diff_args,
    path_in_prefixes,
)

CHECK_ID = "MIL-SCOPE-001"


def _kind(head: Path, allow_by_kind: dict) -> tuple[str | None, bool]:
    """Return (kind, explicit). Explicit=True when pr.yaml or MIL_PR_KIND set it."""
    pr_path = head / "pr.yaml"
    pr = {}
    if pr_path.is_file():
        loaded = load_yaml_path(pr_path)
        if loaded is not None and not isinstance(loaded, dict):
            die(f"{pr_path}: must be a mapping")
        pr = loaded or {}
    explicit_kind = pr.get("kind") or os.environ.get("MIL_PR_KIND")
    if explicit_kind:
        return str(explicit_kind), True
    labels = os.environ.get("MIL_PR_LABELS", "")
    known = set(allow_by_kind)
    for item in labels.split(","):
        item = item.strip()
        if item in known:
            return item, False
    for item in pr.get("labels") or []:
        if str(item) in known:
            return str(item), False
    branch = pr.get("branch") or os.environ.get("MIL_PR_BRANCH") or ""
    prefix = str(branch).split("/", 1)[0]
    if prefix in known:
        return prefix, False
    return None, False


def main(argv: list[str] | None = None) -> int:
    args = parse_diff_args(argv)
    base = Path(args.base)
    head = Path(args.head)
    cfg = check_config(Path(args.config), CHECK_ID)
    allow_by_kind = cfg.get("allow_by_kind") or {}
    if not isinstance(allow_by_kind, dict) or not allow_by_kind:
        die(f"checks.{CHECK_ID}.allow_by_kind is missing")
    kind, explicit = _kind(head, allow_by_kind)
    if kind is None:
        return 0
    if kind not in allow_by_kind:
        if explicit:
            die(f"no allowlist for PR kind {kind!r}")
        return 0
    allow = allow_by_kind[kind] or []
    if not isinstance(allow, list):
        die(f"allowlist for {kind!r} must be a list of path prefixes")
    message = cfg.get("message") or (
        f"PR kind {kind} may only change {', '.join(str(p) for p in allow)}. "
        "Move other paths to a separate pull request."
    )
    hits = 0
    for path in changed_paths(base, head):
        if path_in_prefixes(path, allow):
            continue
        emit(path, message)
        hits += 1
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
