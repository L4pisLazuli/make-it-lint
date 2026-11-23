#!/usr/bin/env python3
"""Claude Code & Cursor Stop hook: suggest recording a failure that was fixed.

Reads the session log written by session_log.py. When a shell command
failed and the same command (or the same test/lint runner family) later
succeeded in this session, it informs the agent once per failure via
hookSpecificOutput.additionalContext (or followup_message for Cursor).
Nothing is recorded without the user's approval.

The hook always exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
ANSWER_ON_EXIT_1 = {
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "diff",
    "cmp",
    "test",
    "[",
    "which",
    "command",
    "false",
    "git diff",
}
RUNNER_FAMILIES = {
    "pytest",
    "ruff",
    "mypy",
    "pyright",
    "flake8",
    "eslint",
    "biome",
    "tsc",
    "vitest",
    "jest",
    "cargo test",
    "cargo check",
    "cargo clippy",
    "go test",
    "go vet",
    "npm test",
    "npm run test",
    "npm run lint",
    "pnpm test",
    "pnpm lint",
    "yarn test",
    "yarn lint",
    "python -m unittest",
    "python3 -m unittest",
    "python -m pytest",
    "python3 -m pytest",
    "bin/mil lint",
    "bin/mil prove",
    "bin/mil ci",
    "mil lint",
    "mil prove",
    "mil ci",
}


def normalize(command: str) -> str:
    return " ".join(command.split())


def _strip_env_assignments(command: str) -> list[str]:
    words = command.split()
    idx = 0
    while idx < len(words) and "=" in words[idx] and not words[idx].startswith("-"):
        idx += 1
    return words[idx:]


def program(command: str) -> str:
    words = _strip_env_assignments(command)
    if not words:
        return ""
    head = os.path.basename(words[0])
    if head == "git" and len(words) > 1:
        return "git " + words[1]
    return head


def runner_family(command: str) -> str | None:
    """Identify a test/lint/typecheck runner family so `pytest -k x` -> `pytest` matches."""
    last = re.split(r"\|\||&&|\||;", command)[-1].strip()
    words = _strip_env_assignments(last)
    if not words:
        return None
    normalized = " ".join([words[0].lstrip("./"), *words[1:]])
    for prefix in sorted(RUNNER_FAMILIES, key=len, reverse=True):
        if normalized == prefix or normalized.startswith(prefix + " "):
            return prefix
    base0 = os.path.basename(words[0])
    rebuilt = " ".join([base0, *words[1:]])
    for prefix in sorted(RUNNER_FAMILIES, key=len, reverse=True):
        if rebuilt == prefix or rebuilt.startswith(prefix + " "):
            return prefix
    return None


def is_answer(row: dict) -> bool:
    if row.get("exit") != 1:
        return False
    cmd = row.get("cmd") or ""
    last = re.split(r"\|\||&&|\||;", cmd)[-1].strip()
    return program(last) in ANSWER_ON_EXIT_1


def fixed_failures(rows: list[dict]) -> list[dict]:
    """Failures whose exact command or test/lint runner family later succeeded."""
    found: list[dict] = []
    pending_exact: dict[str, dict] = {}
    pending_family: dict[str, list[str]] = {}

    for row in rows:
        key = normalize(str(row.get("cmd") or ""))
        if not key:
            continue
        fam = runner_family(key)
        if row.get("ok") is False and not is_answer(row):
            if key not in pending_exact:
                pending_exact[key] = row
                if fam:
                    pending_family.setdefault(fam, []).append(key)
        elif row.get("ok") is True:
            if key in pending_exact:
                found.append(pending_exact.pop(key))
                if fam and fam in pending_family and key in pending_family[fam]:
                    pending_family[fam].remove(key)
            elif fam and pending_family.get(fam):
                for matched_key in list(pending_family.pop(fam, [])):
                    if matched_key in pending_exact:
                        found.append(pending_exact.pop(matched_key))
    return found


def load_rows(path: Path) -> list[dict]:
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_suggested(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(data, list):
        return {str(x) for x in data}
    return set()


def save_suggested(path: Path, seen: set[str]) -> bool:
    try:
        path.write_text(json.dumps(sorted(seen), ensure_ascii=False) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def message(fixed: list[dict]) -> str:
    first = fixed[0]
    cmd = normalize(first.get("cmd") or "")
    if len(cmd) > 120:
        cmd = cmd[:117] + "..."
    detail = f"`{cmd}` failed"
    if first.get("exit") is not None:
        detail += f" (exit {first['exit']})"
    if first.get("error"):
        detail += f": {first['error']}"
    more = f" and {len(fixed) - 1} more" if len(fixed) > 1 else ""
    return (
        "make-it-lint: in this session " + detail + more + ", and later passed. "
        "If the failure came from an agent mistake worth not repeating, briefly offer the user to record it "
        "with the make-it-lint skill (mil record). Do not record it without the user's yes. "
        "If it was an expected or trivial failure, say nothing about it."
    )


def emit(text: str, *, event_name: str = "Stop") -> None:
    if event_name == "stop":
        print(json.dumps({"followup_message": text}, ensure_ascii=False))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}}, ensure_ascii=False))


def _resolve_sessions_dir(data: dict) -> Path | None:
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

    for raw in candidates:
        cur = Path(raw).resolve()
        for parent in (cur, *cur.parents):
            mil = parent / ".make-it-lint"
            if mil.is_dir():
                return mil / "cache" / "sessions"
    return None


def _ensure_utf8_stdout() -> None:
    reconf = getattr(sys.stdout, "reconfigure", None)
    enc = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    if callable(reconf) and enc != "utf8":
        try:
            reconf(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _read_stdin_utf8() -> str:
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return buf.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def main() -> int:
    _ensure_utf8_stdout()
    try:
        data = json.loads(_read_stdin_utf8())
    except (ValueError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("stop_hook_active") or data.get("loop_count", 0):
        return 0
    event_name = str(data.get("hook_event_name") or "Stop")

    legacy = os.environ.get("MIL_SESSION_LOG")
    if legacy:
        log = Path(legacy)
        marker = Path(legacy + ".mil-suggested")
        if not log.is_file() or log.stat().st_size == 0 or marker.exists():
            return 0
        try:
            marker.touch()
        except OSError:
            return 0
        emit(
            "make-it-lint: something failed and was fixed in this session. If it was a mistake worth "
            "not repeating, offer the user to record it with the make-it-lint skill (mil record).",
            event_name=event_name,
        )
        return 0

    directory = _resolve_sessions_dir(data)
    raw_session = data.get("session_id") or data.get("conversation_id") or ""
    session = SAFE_ID.sub("_", str(raw_session))
    if directory is None or not session:
        return 0
    log = directory / f"{session}.ndjson"
    legacy_marker = directory / f"{session}.suggested"
    state_file = directory / f"{session}.suggested.json"
    if not log.is_file() or legacy_marker.exists():
        return 0
    seen = load_suggested(state_file)
    all_fixed = fixed_failures(load_rows(log))
    unseen = [row for row in all_fixed if normalize(str(row.get("cmd") or "")) not in seen]
    if not unseen:
        return 0
    for row in unseen:
        seen.add(normalize(str(row.get("cmd") or "")))
    if not save_suggested(state_file, seen):
        return 0
    emit(message(unseen), event_name=event_name)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - a Stop hook must never break the session
        raise SystemExit(0)
