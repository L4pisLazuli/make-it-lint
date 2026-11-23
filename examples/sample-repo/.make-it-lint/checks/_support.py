"""Helpers for sample checks. Not a check (underscore prefix).

mil exports MIL_CHECK_CONFIG_JSON so checks can read their configuration
with Python's stdlib `json` module. Direct manual invocations fall back
to `mil.yamlio`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable


def load_yaml(text: str) -> Any:
    load = _yaml_load()
    return load(text)


def load_yaml_path(path: Path) -> Any:
    return load_yaml(path.read_text(encoding="utf-8"))


def _yaml_load():
    try:
        from mil.yamlio import load

        return load
    except ImportError:
        pass
    here = Path(__file__).resolve()
    for root in here.parents:
        lib = root / "lib"
        if (lib / "mil" / "yamlio.py").exists():
            sys.path.insert(0, str(lib))
            from mil.yamlio import load

            return load
    sys.stderr.write("cannot import mil.yamlio; copy lib/mil next to this tree or set PYTHONPATH\n")
    raise SystemExit(2)


def parse_tree_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--config", required=True)
    return p.parse_args(argv)


def parse_diff_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--config", required=True)
    return p.parse_args(argv)


def parse_action_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--action", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--config", required=True)
    return p.parse_args(argv)


def check_config(config_path: Path, check_id: str) -> dict[str, Any]:
    env_json = os.environ.get("MIL_CHECK_CONFIG_JSON")
    if env_json:
        try:
            parsed = json.loads(env_json)
            if isinstance(parsed, dict) and parsed:
                return parsed
        except ValueError:
            pass
    data = load_yaml_path(config_path)
    if not isinstance(data, dict):
        die(f"{config_path}: config must be a mapping")
    checks = data.get("checks") or {}
    if not isinstance(checks, dict) or check_id not in checks:
        die(f"{config_path}: missing checks.{check_id}")
    section = checks[check_id]
    if section is None:
        return {}
    if not isinstance(section, dict):
        die(f"{config_path}: checks.{check_id} must be a mapping")
    return section


def is_ledger_fixture(rel: str) -> bool:
    """True for paths under .make-it-lint/ledger/F-.../bad/ or good/."""
    try:
        from mil.paths import is_ledger_fixture_path

        return is_ledger_fixture_path(rel)
    except ImportError:
        norm = rel.replace("\\", "/")
        return ".make-it-lint/ledger/" in norm and (
            "/bad/" in norm or norm.endswith("/bad") or "/good/" in norm or norm.endswith("/good")
        )


def rel_files(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        if is_ledger_fixture(rel):
            continue
        out[rel] = path
    return out


def changed_paths(base: Path, head: Path) -> list[str]:
    before = rel_files(base)
    after = rel_files(head)
    names = set(before) | set(after)
    changed = []
    for name in sorted(names):
        if name in ("pr.yaml",):
            continue
        a, b = before.get(name), after.get(name)
        if a is None or b is None:
            changed.append(name)
            continue
        if a.read_bytes() != b.read_bytes():
            changed.append(name)
    return changed


def path_in_prefixes(path: str, prefixes: Iterable[str]) -> bool:
    norm = path.replace("\\", "/")
    for raw in prefixes:
        prefix = str(raw).replace("\\", "/").rstrip("/")
        if not prefix:
            continue
        if norm == prefix or norm.startswith(prefix + "/"):
            return True
    return False


def die(message: str, code: int = 2) -> None:
    sys.stderr.write(message + "\n")
    raise SystemExit(code)


def emit(path: str, message: str, line: int | None = None) -> None:
    if line is None:
        print(f"{path}: {message}")
    else:
        print(f"{path}:{line}: {message}")
