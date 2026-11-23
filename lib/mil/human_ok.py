"""A person at a terminal must confirm `mil promote`.

`--approved-by` is a name written into the ledger, not a gate: a shell
agent could pass any string. This module enforces the gate: known agent
runtimes are refused, stdin must be an interactive terminal, and the
person must type the exact target state (`shadow`, `comment`, or `block`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from mil.errors import MilError, mil_line

AGENT_ENV_VARS = (
    "CURSOR_AGENT",
    "CLAUDECODE",
    "CLAUDE_CODE",
    "CODEX_SANDBOX",
    "AIDER",
)


def agent_runtime(env: Mapping[str, str] | None = None) -> str | None:
    """Return the first non-empty agent marker set in `env`, or None."""
    source = os.environ if env is None else env
    for name in AGENT_ENV_VARS:
        value = source.get(name)
        if value is not None and str(value).strip() != "":
            return name
    return None


def approver_looks_like_agent(name: str) -> bool:
    return "agent" in name.lower()


def confirm_promote(
    target: str,
    *,
    rid: str,
    current: str,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    isatty: bool | None = None,
    stderr: TextIO | None = None,
) -> None:
    """Refuse unless a person types `target` at a real terminal."""
    marker = agent_runtime(env)
    if marker is not None:
        raise MilError(
            mil_line(
                "promote",
                f"refusing to run in an agent environment ({marker})",
                "a person must run mil promote in their own terminal and type the next state",
            )
        )
    stream = sys.stdin if stdin is None else stdin
    err = sys.stderr if stderr is None else stderr
    tty = stream.isatty() if isatty is None else isatty
    if not tty:
        raise MilError(
            mil_line(
                "promote",
                "stdin is not a terminal",
                f"type {target} at the prompt; a pipe is not a human OK",
            )
        )
    print(f"mil promote: {rid} {current} -> {target}", file=err)
    print(f"type {target} to confirm:", file=err)
    err.flush()
    try:
        line = stream.readline()
    except EOFError as exc:
        raise MilError(
            mil_line(
                "promote",
                "confirmation did not match",
                f"type {target} exactly",
            )
        ) from exc
    if not isinstance(line, str) or line.strip() != target:
        raise MilError(
            mil_line(
                "promote",
                "confirmation did not match",
                f"type {target} exactly",
            )
        )


def drop_agent_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `env` with known agent runtime markers removed."""
    out = dict(env)
    for name in AGENT_ENV_VARS:
        out.pop(name, None)
    return out


def history_entry(
    *,
    target: str,
    current: str,
    approved_by: str,
    at: str,
    note: str | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "state": target,
        "at": at,
        "from_state": current,
        "approved_by": approved_by,
    }
    if note:
        entry["note"] = note
    return entry
