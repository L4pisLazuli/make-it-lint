"""Hunk-revert verification for mil prove --check-hunks.

Reverts one fix hunk at a time from good/ back toward bad/. When good/
still passes (exit 0) for a non-comment hunk, warns that the check may
not cover that part of the fix.
"""

from __future__ import annotations

import ast
import difflib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mil.checks import CheckError, invoke_check_reproducible
from mil.fixtures import is_multi_fixture, list_bad_variants, list_good_variants

HUNK_TIMEOUT = 30


@dataclass
class FileHunk:
    where: str
    apply_fn: Any

    def apply(self, dest: Path) -> None:
        self.apply_fn(dest)


def hunk_warnings_for(
    program: Path,
    metadata: dict[str, Any],
    root: Path,
    config: Path,
    bad: Path,
    good: Path,
    *,
    seed: int,
    layer: str | None,
) -> list[str]:
    """Revert one fix hunk at a time onto good/. Staying green is a warning."""
    warnings: list[str] = []
    multi = is_multi_fixture(bad, good)
    bads = list_bad_variants(bad, multi=multi)
    goods = list_good_variants(good, multi=multi)
    if not bads or not goods:
        return warnings
    if len(bads) == len(goods):
        pairs = list(zip(bads, goods))
    else:
        pairs = [(bads[0], g) for g in goods]
    for (_blabel, bdir), (glabel, gdir) in pairs:
        for hunk in tree_hunks(bdir, gdir):
            tmp = Path(tempfile.mkdtemp(prefix="mil-hunk-"))
            skip_warn = False
            proc = None
            try:
                shutil.copytree(gdir, tmp, dirs_exist_ok=True)
                hunk.apply(tmp)
                proc = invoke_check_reproducible(
                    program,
                    metadata,
                    layer=layer,
                    root=root,
                    config=config,
                    fixture_dir=tmp,
                    seed=seed,
                    timeout=HUNK_TIMEOUT,
                )
                if proc.returncode == 0 and _same_code_payload(gdir, tmp):
                    skip_warn = True
            except (CheckError, OSError, shutil.Error) as exc:
                warnings.append("hunk revert %s %s: %s" % (glabel, hunk.where, exc))
                continue
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if proc is not None and proc.returncode == 0 and not skip_warn:
                warnings.append(
                    "hunk revert stayed green on %s %s; the check may not see this fix"
                    % (glabel, hunk.where)
                )
    return warnings


def tree_hunks(bad: Path, good: Path) -> list[FileHunk]:
    hunks: list[FileHunk] = []
    bad_files = _rel_files(bad)
    good_files = _rel_files(good)
    names = sorted(set(bad_files) | set(good_files))
    for name in names:
        b = bad_files.get(name)
        g = good_files.get(name)
        if b is None and g is not None:
            hunks.append(FileHunk(where="%s (added in good)" % name, apply_fn=_delete_rel(name)))
            continue
        if g is None and b is not None:
            hunks.append(
                FileHunk(where="%s (removed in good)" % name, apply_fn=_restore_rel(name, b.read_bytes()))
            )
            continue
        if b is None or g is None:
            continue
        if b.read_bytes() == g.read_bytes():
            continue
        try:
            bad_text = b.read_text(encoding="utf-8")
            good_text = g.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            hunks.append(
                FileHunk(where="%s (binary)" % name, apply_fn=_restore_rel(name, b.read_bytes()))
            )
            continue
        bad_lines = bad_text.splitlines(keepends=True)
        good_lines = good_text.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, bad_lines, good_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            hunks.append(
                FileHunk(
                    where="%s lines %d-%d" % (name, j1 + 1, max(j2, j1 + 1)),
                    apply_fn=_replace_lines(name, j1, j2, bad_lines[i1:i2]),
                )
            )
    return hunks


def _same_code_payload(left: Path, right: Path) -> bool:
    """True when two trees differ only in comments or docstrings."""
    left_files = _rel_files(left)
    right_files = _rel_files(right)
    names = set(left_files) | set(right_files)
    for name in names:
        if _code_payload(left_files.get(name)) != _code_payload(right_files.get(name)):
            return False
    return True


def _code_payload(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return path.read_bytes().hex()
    shebang = text.lstrip().splitlines()[:1]
    if path.suffix == ".py" or (shebang and "python" in shebang[0]):
        return _python_payload(text)
    return _hash_comment_payload(text)


def _hash_comment_payload(text: str) -> str:
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def _python_payload(text: str) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _hash_comment_payload(text)
    _drop_docstrings(tree)
    ast.fix_missing_locations(tree)
    try:
        return ast.unparse(tree).strip()
    except Exception:  # noqa: BLE001 — fall back to comment stripping
        return _hash_comment_payload(text)


def _drop_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:]
        if not node.body:
            node.body = [ast.Pass()]


def _rel_files(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for path in root.rglob("*"):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path
    return out


def _delete_rel(name: str):
    def apply(dest: Path) -> None:
        target = dest / name
        if target.is_file():
            target.unlink()

    return apply


def _restore_rel(name: str, data: bytes):
    def apply(dest: Path) -> None:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    return apply


def _replace_lines(name: str, j1: int, j2: int, insert: list[str]):
    def apply(dest: Path) -> None:
        target = dest / name
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
        lines = text.splitlines(keepends=True)
        new = lines[:j1] + insert + lines[j2:]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(new), encoding="utf-8")

    return apply
