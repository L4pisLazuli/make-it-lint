#!/usr/bin/env python3
"""Claude Code & Cursor PreToolUse hook for make-it-lint.

Blocks direct agent file writes to protected governance paths
(.make-it-lint/waivers.yaml, existing checks, existing record.yaml files,
kit-owned files, and configured protected_paths) and runs active
`input: action` checks against the tool call.

Always exits 0 and communicates decisions via structured hook JSON so an
absent or unconfigured repository is never disrupted.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _find_project_root(payload: dict) -> Path | None:
    candidates: list[str] = []
    for env_key in ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR"):
        val = os.environ.get(env_key)
        if val:
            candidates.append(val)
    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        for item in roots:
            if isinstance(item, str) and item:
                candidates.append(item)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        candidates.append(cwd)

    for raw in candidates:
        cur = Path(raw).resolve()
        for parent in (cur, *cur.parents):
            if (parent / ".make-it-lint").is_dir():
                return parent
    return None


def _read_stdin_utf8() -> str:
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return buf.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def main() -> int:
    raw_stdin = _read_stdin_utf8()
    if not raw_stdin.strip():
        return 0
    try:
        payload = json.loads(raw_stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    root = _find_project_root(payload)
    if root is None:
        return 0

    lib_dir = root / "lib"
    if not (lib_dir / "mil").is_dir():
        return 0
    sys.path.insert(0, str(lib_dir))

    from io import StringIO

    from mil.cli import main as mil_main

    prev_stdin = sys.stdin
    try:
        sys.stdin = StringIO(raw_stdin)
        mil_main(["guard", "--hook", "--root", str(root)])
    finally:
        sys.stdin = prev_stdin
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - hook must fail open rather than crash the editor
        raise SystemExit(0)
