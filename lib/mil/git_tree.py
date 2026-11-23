"""Materialize clean git trees from refs or the index without tar or worktrees."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from mil.errors import MilError, mil_line


def _git_bin() -> str:
    git = shutil.which("git")
    if not git:
        raise MilError(mil_line("git", "git executable not found on PATH"))
    return git


def ensure_git_repo(root: Path) -> None:
    git = _git_bin()
    proc = subprocess.run(
        [git, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if proc.returncode != 0:
        raise MilError(mil_line(root, "not a git working tree"))


def has_head(root: Path) -> bool:
    git = _git_bin()
    proc = subprocess.run(
        [git, "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    return proc.returncode == 0


def current_branch(root: Path) -> str | None:
    git = _git_bin()
    proc = subprocess.run(
        [git, "-C", str(root), "symbolic-ref", "-q", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    return branch or None


def has_staged_mil_config(root: Path) -> bool:
    git = _git_bin()
    proc = subprocess.run(
        [git, "-C", str(root), "ls-files", "--error-unmatch", ".make-it-lint/config.yaml"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    return proc.returncode == 0


def materialize_staged(root: Path, dest: Path) -> None:
    """Export the current git index into `dest`."""
    ensure_git_repo(root)
    git = _git_bin()
    dest.mkdir(parents=True, exist_ok=True)
    prefix = str(dest.resolve())
    if not prefix.endswith(os.sep):
        prefix += os.sep
    proc = subprocess.run(
        [git, "-C", str(root), "checkout-index", "-a", "-f", f"--prefix={prefix}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise MilError(mil_line("git checkout-index", detail))


def materialize_ref(root: Path, ref: str, dest: Path) -> None:
    """Export any commit/tree-ish `ref` into `dest` via a temporary index file."""
    ensure_git_repo(root)
    git = _git_bin()
    dest.mkdir(parents=True, exist_ok=True)
    prefix = str(dest.resolve())
    if not prefix.endswith(os.sep):
        prefix += os.sep
    with tempfile.TemporaryDirectory(prefix="mil-git-idx-") as tmp:
        idx_path = str(Path(tmp) / "index")
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = idx_path
        read_proc = subprocess.run(
            [git, "-C", str(root), "read-tree", ref],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if read_proc.returncode != 0:
            detail = (read_proc.stderr or read_proc.stdout or f"unknown ref {ref!r}").strip()
            raise MilError(mil_line("git read-tree", f"{ref}: {detail}"))
        out_proc = subprocess.run(
            [git, "-C", str(root), "checkout-index", "-a", "-f", f"--prefix={prefix}"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if out_proc.returncode != 0:
            detail = (out_proc.stderr or out_proc.stdout or f"exit {out_proc.returncode}").strip()
            raise MilError(mil_line("git checkout-index", f"{ref}: {detail}"))
