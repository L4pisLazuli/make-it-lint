"""Locations of the install tree and an adopter's .make-it-lint directory."""

from __future__ import annotations

import re
from pathlib import Path

LEDGER_FIXTURE_RE = re.compile(
    r"\.make-it-lint/ledger/F-[0-9]{8}-[0-9]{2,}/(?:bad|good)(?:/|$)"
)


def install_root() -> Path:
    return Path(__file__).resolve().parents[2]


def schema_dir() -> Path:
    return install_root() / "schema"


def contract_doc_path() -> Path:
    return install_root() / "docs" / "contract.md"


def contract_doc_rel() -> str:
    path = contract_doc_path()
    try:
        return str(path.resolve().relative_to(install_root()))
    except ValueError:
        return str(path)


def as_rel(value: str) -> str:
    return str(value).rstrip("/")


def normalize_rel_path(raw: str | Path) -> str:
    """Normalize a relative path to POSIX separators without stripping leading dots."""
    text = str(raw).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def is_within_root(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


def mil_dir(root: Path) -> Path:
    return root / ".make-it-lint"


def config_path(root: Path) -> Path:
    return mil_dir(root) / "config.yaml"


def waivers_path(root: Path) -> Path:
    return mil_dir(root) / "waivers.yaml"


def ledger_dir(root: Path) -> Path:
    return mil_dir(root) / "ledger"


def checks_dir(root: Path) -> Path:
    return mil_dir(root) / "checks"


def cache_dir(root: Path) -> Path:
    return mil_dir(root) / "cache"


def events_path(root: Path) -> Path:
    return mil_dir(root) / "events.ndjson"


def is_ledger_fixture_path(rel: str | Path) -> bool:
    """True when a product-tree path sits inside a ledger bad/ or good/ fixture."""
    norm = str(rel).replace("\\", "/")
    return bool(LEDGER_FIXTURE_RE.search(norm))
