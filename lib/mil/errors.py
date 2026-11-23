"""CLI error type and the one-line error format.

Messages look like `mil: <path>: <what>: <why>`: lowercase, no trailing
period. A hint is only added when it names the actual fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class MilError(Exception):
    """User-facing failure. The CLI prints str(exc) on stderr and exits 2."""


def mil_line(*parts: Any) -> str:
    bits = ["mil"]
    for part in parts:
        if part is None or part == "":
            continue
        bits.append(str(part).rstrip("."))
    return ": ".join(bits)


def rel_to(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)
