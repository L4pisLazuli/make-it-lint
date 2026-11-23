#!/usr/bin/env python3
# ---
# id: MIL-SUITE-001
# incident: F-20260518-01
# input: tree
# layer: test
# tool: script
# ---
"""Fail when a suite runner records failures and still exits 0.

A test runner that prints failures then calls sys.exit(0) is green on a
machine that ran zero passing cases.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _support import check_config, emit, is_ledger_fixture, parse_tree_args  # noqa: E402

CHECK_ID = "MIL-SUITE-001"
FAIL_NAMES = {"fail", "fails", "failed", "failures", "errors", "error_count"}


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Tuple):
        return None
    return None


def _assigns_fail_name(node: ast.AST) -> bool:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets.extend(node.targets)
    elif isinstance(node, ast.AnnAssign) and node.target is not None:
        targets.append(node.target)
    elif isinstance(node, ast.AugAssign):
        targets.append(node.target)
    elif isinstance(node, ast.For):
        targets.append(node.target)
    for target in targets:
        name = _name_of(target)
        if name and name.lower() in FAIL_NAMES:
            return True
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                n = _name_of(elt)
                if n and n.lower() in FAIL_NAMES:
                    return True
    return False


def _is_exit_zero(node: ast.AST) -> bool:
    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
        func = node.exc.func
        if isinstance(func, ast.Name) and func.id == "SystemExit":
            return _const_zero(node.exc.args)
    if isinstance(node, ast.Call):
        func = node.func
        called = None
        if isinstance(func, ast.Name):
            called = func.id
        elif isinstance(func, ast.Attribute):
            called = func.attr
        if called in {"exit", "_exit"}:
            return _const_zero(node.args)
    return False


def _const_zero(args: list[ast.expr]) -> bool:
    if not args:
        return True
    arg = args[0]
    if isinstance(arg, ast.Constant) and arg.value == 0:
        return True
    return False


def _scan_file(path: Path) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    hits: list[int] = []

    def walk_fn(fn: ast.AST) -> None:
        has_fail = False
        for child in ast.walk(fn):
            if child is fn:
                continue
            if _assigns_fail_name(child):
                has_fail = True
        if not has_fail:
            return
        for child in ast.walk(fn):
            if _is_exit_zero(child):
                hits.append(getattr(child, "lineno", 1))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            walk_fn(node)
    walk_fn(tree)
    return sorted(set(hits))


def main(argv: list[str] | None = None) -> int:
    args = parse_tree_args(argv)
    root = Path(args.root)
    cfg = check_config(Path(args.config), CHECK_ID)
    names = cfg.get("runner_names") or ["run_suite.py"]
    message = cfg.get("message") or (
        "suite runner ignores failures (sys.exit(0) after recording failed cases)"
    )
    n_hits = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name not in names:
            continue
        rel = path.relative_to(root).as_posix()
        if is_ledger_fixture(rel):
            continue
        for lineno in _scan_file(path):
            emit(rel, message, line=lineno)
            n_hits += 1
    return 1 if n_hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
