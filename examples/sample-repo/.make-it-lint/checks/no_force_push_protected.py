#!/usr/bin/env python3
# ---
# id: MIL-HOOK-001
# incident: F-20260905-01
# input: action
# layer: hook
# tool: hook
# ---
"""Fail when a shell tool call force-pushes to a protected branch.

Tree and diff checks run after a command has already executed; destructive
remote git operations must be stopped at PreToolUse (`input: action`).
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _support import check_config, die, emit, parse_action_args  # noqa: E402

CHECK_ID = "MIL-HOOK-001"
FORCE_FLAGS = ("--force", "-f", "--force-with-lease", "--force-if-includes")


def _matches_protected(branch: str, patterns: list[str]) -> bool:
    clean = branch.removeprefix("refs/heads/")
    return any(fnmatch.fnmatchcase(clean, str(pat)) for pat in patterns)


def _check_segment(segment: str, patterns: list[str]) -> list[str]:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens.pop(0)
    if len(tokens) < 2 or Path(tokens[0]).name != "git":
        return []
    idx = 1
    while idx < len(tokens) and tokens[idx].startswith("-"):
        if tokens[idx] in {"-C", "-c", "--git-dir", "--work-tree"}:
            idx += 2
        else:
            idx += 1
    if idx >= len(tokens) or tokens[idx] != "push":
        return []
    push_args = tokens[idx + 1 :]
    has_force_flag = any(
        arg in FORCE_FLAGS or any(arg.startswith(flag + "=") for flag in FORCE_FLAGS)
        for arg in push_args
    )
    positionals = [arg for arg in push_args if not arg.startswith("-")]
    refspecs = positionals[1:] if len(positionals) >= 2 else []
    hits: list[str] = []
    for spec in refspecs:
        forced_refspec = spec.startswith("+")
        raw_spec = spec.lstrip("+")
        target = raw_spec.split(":", 1)[-1]
        if (has_force_flag or forced_refspec) and _matches_protected(target, patterns):
            hits.append(target.removeprefix("refs/heads/"))
    return hits


def main(argv: list[str] | None = None) -> int:
    args = parse_action_args(argv)
    action_path = Path(args.action)
    try:
        action = json.loads(action_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"{action_path}: invalid action JSON: {exc}")
    if not isinstance(action, dict):
        die(f"{action_path}: action payload must be a JSON object")
    command = str(action.get("command") or "").strip()
    if not command:
        return 0
    cfg = check_config(Path(args.config), CHECK_ID)
    protected = [str(p) for p in (cfg.get("protected_branches") or ["main"])]
    segments = re.split(r"&&|\|\||;|\n", command)
    flagged: list[str] = []
    for seg in segments:
        flagged.extend(_check_segment(seg.strip(), protected))
    if not flagged:
        return 0
    for branch in flagged:
        emit(
            f"git-push/{branch}",
            f"force-pushing to protected branch {branch!r} is forbidden; push a feature branch instead",
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
