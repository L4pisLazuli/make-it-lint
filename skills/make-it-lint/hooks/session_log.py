#!/usr/bin/env python3
"""Claude Code & Cursor PostToolUse / PostToolUseFailure hook for shell commands.

Appends one JSON line per Bash/Shell call to
.make-it-lint/cache/sessions/<session_id>.ndjson: the command, whether
it succeeded, and for a failure the exit code and first error line.
stop_suggest.py reads that file. The hook never prints and always
exits 0, so it cannot block or slow down a tool call in a visible way.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

EXIT_RE = re.compile(r"^Exit code (\d+)")
SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
SHELL_TOOLS = {"Bash", "Shell", "RunCommand", "Terminal"}


def log_dir(data: dict) -> Path | None:
    candidates: list[str] = []
    for env_key in ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR"):
        val = os.environ.get(env_key)
        if val:
            candidates.append(val)
    roots = data.get("workspace_roots")
    if isinstance(roots, list):
        for item in roots:
            if isinstance(item, str) and item:
                candidates.append(item)
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        candidates.append(cwd)

    for base in candidates:
        cur = Path(base).resolve()
        for parent in (cur, *cur.parents):
            mil = parent / ".make-it-lint"
            if mil.is_dir():
                return mil / "cache" / "sessions"
    return None


def entry(data: dict) -> dict | None:
    event = str(data.get("hook_event_name") or "")
    tool = str(data.get("tool_name") or ("Shell" if event == "afterShellExecution" else ""))
    if tool and tool not in SHELL_TOOLS:
        return None
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or data.get("command") or "").strip()
    if not command:
        return None

    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "cmd": command}
    if event in ("PostToolUseFailure", "postToolUseFailure"):
        if data.get("is_interrupt"):
            return None
        error = str(data.get("error") or data.get("failure_type") or "")
        lines = [ln for ln in error.splitlines() if ln.strip()]
        match = EXIT_RE.match(lines[0]) if lines else None
        row["ok"] = False
        row["exit"] = int(match.group(1)) if match else _int_or_none(data.get("exit_code"))
        rest = lines[1:] if match else lines
        row["error"] = rest[0][:200] if rest else ""
        return row

    if event in ("PostToolUse", "postToolUse", "afterShellExecution"):
        exit_code = _int_or_none(data.get("exit_code"))
        response = data.get("tool_response") if "tool_response" in data else data.get("tool_output")
        if isinstance(response, dict) and exit_code is None:
            exit_code = _int_or_none(response.get("exit_code") if "exit_code" in response else response.get("returncode"))
        if exit_code is not None and exit_code != 0:
            row["ok"] = False
            row["exit"] = exit_code
            out_text = str(data.get("output") or (response.get("stderr") or response.get("stdout") if isinstance(response, dict) else response) or "")
            lines = [ln for ln in out_text.splitlines() if ln.strip()]
            row["error"] = lines[0][:200] if lines else ""
            return row
        if isinstance(response, str) and event == "PostToolUse":
            row["ok"] = False
            row["exit"] = None
            row["error"] = response.strip().splitlines()[0][:200] if response.strip() else ""
        else:
            row["ok"] = True
        return row
    return None


def _int_or_none(val: object) -> int | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_stdin_utf8() -> str:
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return buf.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def main() -> int:
    try:
        data = json.loads(_read_stdin_utf8())
    except (ValueError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    row = entry(data)
    directory = log_dir(data)
    raw_session = data.get("session_id") or data.get("conversation_id") or ""
    session = SAFE_ID.sub("_", str(raw_session))
    if row is None or directory is None or not session:
        return 0
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / f"{session}.ndjson", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - a logging hook must never break the session
        raise SystemExit(0)
