"""Discover checks, read leading metadata, and invoke them per the contract."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from mil import yamlio
from mil.errors import rel_to
from mil.models import CHECK_INPUTS
from mil.paths import checks_dir, config_path, install_root, is_ledger_fixture_path, is_within_root

META_FENCE = "---"
PR_ENV = ("MIL_PR_BRANCH", "MIL_PR_LABELS", "MIL_PR_KIND")
WIN_EXEC_SUFFIXES = {".py", ".sh", ".exe", ".cmd", ".bat", ".ps1"}


class CheckError(RuntimeError):
    pass


class NonReproducibleError(CheckError):
    """Raised when a `layer: verify` check produces different output across two seeded runs."""


def extract_metadata(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    yaml_text = _leading_yaml(text)
    if yaml_text is None:
        raise CheckError(f"{path}: no YAML metadata in the leading comment")
    data = yamlio.load(yaml_text)
    if not isinstance(data, dict):
        raise CheckError(f"{path}: metadata must be a mapping")
    if "input" not in data:
        data["input"] = "tree"
    return data


def _leading_yaml(text: str) -> str | None:
    lines = text.splitlines()
    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    if i < len(lines) and re.match(r"^#.*coding[:=]", lines[i]):
        i += 1
    if i < len(lines) and _hash_body(lines[i]) == META_FENCE:
        i += 1
        block: list[str] = []
        while i < len(lines):
            body = _hash_body(lines[i])
            if body is None:
                break
            if body == META_FENCE:
                return "\n".join(block) + "\n"
            block.append(body)
            i += 1
        return None
    if i < len(lines) and lines[i].strip()[:3] in ('"""', "'''"):
        quote = lines[i].strip()[:3]
        first = lines[i].strip()[3:]
        doc_lines: list[str] = []
        if first.endswith(quote) and len(first) > 3:
            doc_lines = [first[:-3]]
        else:
            if first:
                doc_lines.append(first)
            i += 1
            while i < len(lines) and quote not in lines[i]:
                doc_lines.append(lines[i])
                i += 1
        return _yaml_between_fences(doc_lines)
    block = []
    while i < len(lines):
        body = _hash_body(lines[i])
        if body is None or body == "":
            break
        if ":" not in body and not body.startswith("- "):
            break
        block.append(body)
        i += 1
    if block:
        return "\n".join(block) + "\n"
    return None


def _hash_body(line: str) -> str | None:
    if not line.startswith("#"):
        return None
    if line.startswith("# "):
        return line[2:]
    if line == "#":
        return ""
    return line[1:]


def _yaml_between_fences(lines: list[str]) -> str | None:
    stripped = [ln.rstrip() for ln in lines]
    try:
        start = next(i for i, ln in enumerate(stripped) if ln.strip() == META_FENCE)
        end = next(i for i, ln in enumerate(stripped[start + 1 :], start + 1) if ln.strip() == META_FENCE)
    except StopIteration:
        return None
    return "\n".join(stripped[start + 1 : end]) + "\n"


def resolve_check_program(path: Path) -> Path:
    if path.is_file():
        return path
    if path.is_dir():
        matches = sorted(p for p in path.glob("check.*") if p.is_file())
        plain = path / "check"
        if plain.is_file():
            matches.append(plain)
        if not matches:
            raise CheckError(f"{path}: directory has no check.* program")
        if len(matches) > 1:
            names = ", ".join(p.name for p in matches)
            raise CheckError(f"{path}: multiple check programs ({names})")
        return matches[0]
    raise CheckError(f"{path}: check does not exist")


def check_entries(root: Path) -> list[Path]:
    """Entries of .make-it-lint/checks/ that are checks, skipping helpers."""
    base = checks_dir(root)
    if not base.is_dir():
        return []
    return [
        child
        for child in sorted(base.iterdir())
        if not child.name.startswith((".", "_")) and child.name != "README.md"
    ]


def discover_checks(
    root: Path,
) -> tuple[list[tuple[Path, dict[str, Any]]], list[tuple[Path, Exception]]]:
    """Return (program, metadata) for each check, and (entry, error) for unreadable entries."""
    found: list[tuple[Path, dict[str, Any]]] = []
    errors: list[tuple[Path, Exception]] = []
    for child in check_entries(root):
        try:
            program = resolve_check_program(child)
            found.append((program, extract_metadata(program)))
        except (CheckError, yamlio.YAMLError) as exc:
            errors.append((child, exc))
    return found, errors


def git_non_exec_checks(root: Path) -> set[Path]:
    """Return resolved paths of tracked files under `.make-it-lint/checks` without git mode 100755."""
    git = shutil.which("git")
    if not git:
        return set()
    proc = subprocess.run(
        [git, "-C", str(root), "ls-files", "-s", ".make-it-lint/checks"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if proc.returncode != 0:
        return set()
    non_exec: set[Path] = set()
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        mode = parts[0].split()[0]
        rel = parts[1]
        if mode != "100755":
            non_exec.add((root / rel).resolve())
    return non_exec


def check_executable_issue(
    root: Path,
    program: Path,
    git_non_exec: set[Path] | None = None,
) -> str | None:
    """Return a human-readable problem if `program` is not executable across OSes and git."""
    if os.name != "nt":
        if not os.access(program, os.X_OK) or bool(program.stat().st_mode & stat.S_IFDIR):
            return "is not executable (chmod +x); runners call it directly"
    else:
        if program.stat().st_mode & stat.S_IFDIR:
            return "is a directory, not an executable check"
        if _shebang(program) is None and program.suffix.lower() not in WIN_EXEC_SUFFIXES:
            return "is not executable on Windows (add a #! shebang or use .py/.sh/.cmd/.bat/.ps1/.exe)"

    non_exec = git_non_exec if git_non_exec is not None else git_non_exec_checks(root)
    if program.resolve() in non_exec:
        return f"is tracked in git without +x; run git update-index --chmod=+x {rel_to(root, program)}"
    return None


def ensure_check_executable(root: Path, program: Path) -> str | None:
    """Ensure a check under `.make-it-lint/checks/` is +x on disk and in the git index if tracked."""
    if not program.is_file() or not is_within_root(checks_dir(root), program):
        return None
    if os.name != "nt" and not os.access(program, os.X_OK):
        try:
            mode = program.stat().st_mode
            program.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass

    rel_posix = rel_to(root, program)
    git = shutil.which("git")
    if not git:
        return None
    ls_proc = subprocess.run(
        [git, "-C", str(root), "ls-files", "-s", "--", rel_posix],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if ls_proc.returncode == 0 and ls_proc.stdout.strip():
        first_line = ls_proc.stdout.splitlines()[0]
        parts = first_line.split("\t", 1)
        if len(parts) == 2 and parts[0].split()[0] != "100755":
            subprocess.run(
                [git, "-C", str(root), "update-index", "--chmod=+x", "--", rel_posix],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
        return None

    if os.name == "nt":
        wt_proc = subprocess.run(
            [git, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
        )
        if wt_proc.returncode == 0:
            return f"note: on Windows, stage with executable bit: git add --chmod=+x {rel_posix}"
    return None


def load_pr_yaml(fixture_dir: Path) -> dict[str, Any] | None:
    path = fixture_dir / "pr.yaml"
    if not path.is_file():
        return None
    data = yamlio.load_path(str(path))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CheckError(f"{path}: pr.yaml must be a mapping")
    return data


def _load_config_dict(config: Path) -> dict[str, Any]:
    if not config.is_file():
        return {}
    try:
        data = yamlio.load_path(str(config))
    except yamlio.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def invoke_check(
    program: Path,
    metadata: dict[str, Any],
    *,
    root: Path,
    config: Path,
    fixture_dir: Path | None = None,
    base_dir: Path | None = None,
    head_dir: Path | None = None,
    action_file: Path | None = None,
    timeout: int = 60,
    seed: int | None = None,
    config_data: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    kind = metadata.get("input") or "tree"
    if kind not in CHECK_INPUTS:
        raise CheckError(f"{program}: unknown input {kind!r}")
    if kind == "diff":
        if fixture_dir is not None:
            base = fixture_dir / "base"
            head = fixture_dir / "head"
            if not base.is_dir() or not head.is_dir():
                raise CheckError(f"{fixture_dir}: diff fixtures need base/ and head/")
            pr = load_pr_yaml(fixture_dir) or load_pr_yaml(head)
        elif base_dir is not None and head_dir is not None:
            base = Path(base_dir)
            head = Path(head_dir)
            if not base.is_dir() or not head.is_dir():
                raise CheckError("diff check needs --base and --head directories")
            pr = load_pr_yaml(head)
        else:
            raise CheckError("diff check needs a fixture directory with base/ and head/")
        args = ["--base", str(base), "--head", str(head), "--config", str(config)]
    elif kind == "action":
        if fixture_dir is not None:
            act = fixture_dir / "action.json"
            if not act.is_file():
                raise CheckError(f"{fixture_dir}: action fixtures need action.json")
            target_root = fixture_dir
            pr = load_pr_yaml(fixture_dir)
        elif action_file is not None:
            act = Path(action_file)
            if not act.is_file():
                raise CheckError(f"{act}: action file does not exist")
            target_root = root
            pr = None
        else:
            raise CheckError("action check needs --action or a fixture directory with action.json")
        args = ["--action", str(act), "--root", str(target_root), "--config", str(config)]
    else:
        tree = fixture_dir if fixture_dir is not None else root
        args = ["--root", str(tree), "--config", str(config)]
        pr = load_pr_yaml(tree) if fixture_dir is not None else None

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    lib = str(install_root() / "lib")
    env["PYTHONPATH"] = lib + os.pathsep + env.get("PYTHONPATH", "")

    cfg_dict = config_data if config_data is not None else _load_config_dict(config)
    check_id = str(metadata.get("id") or "")
    checks_map = cfg_dict.get("checks") if isinstance(cfg_dict.get("checks"), dict) else {}
    section = checks_map.get(check_id) if check_id and isinstance(checks_map, dict) else {}
    if not isinstance(section, dict):
        section = {}
    env["MIL_CONFIG_JSON"] = json.dumps(cfg_dict, ensure_ascii=False, separators=(",", ":"))
    env["MIL_CHECK_CONFIG_JSON"] = json.dumps(section, ensure_ascii=False, separators=(",", ":"))

    if fixture_dir is not None:
        for name in PR_ENV:
            env.pop(name, None)
        env.pop("MIL_SEED", None)
    env["MIL_SEED"] = str(0 if seed is None else seed)
    if pr:
        branch = pr.get("branch")
        if branch:
            env["MIL_PR_BRANCH"] = str(branch)
        labels = pr.get("labels", [])
        if isinstance(labels, list):
            env["MIL_PR_LABELS"] = ",".join(str(x) for x in labels)
        elif labels:
            env["MIL_PR_LABELS"] = str(labels)
        kind_value = pr.get("kind")
        if kind_value:
            env["MIL_PR_KIND"] = str(kind_value)
    cmd = _program_argv(program) + args
    try:
        return subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except OSError as exc:
        raise CheckError(f"{program}: failed to execute {cmd[0]!r}: {exc}") from exc


def _program_argv(program: Path) -> list[str]:
    shebang_interp = _shebang(program)
    if shebang_interp:
        return _normalize_shebang(shebang_interp, program) + [str(program)]
    suffix = program.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(program)]
    if suffix == ".sh":
        return [_resolve_shell("sh", program), str(program)]
    if os.name == "nt":
        if suffix in (".cmd", ".bat"):
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(program)]
        if suffix == ".ps1":
            ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(program)]
    return [str(program)]


def _shebang(program: Path) -> list[str] | None:
    try:
        first = program.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("#!"):
        return None
    return first[2:].strip().split()


def _resolve_shell(shell_name: str, program: Path) -> str:
    if os.name != "nt" and shell_name == "sh" and os.path.isfile("/bin/sh"):
        return "/bin/sh"
    found = shutil.which(shell_name)
    if found:
        return found
    raise CheckError(f"{program}: POSIX shell {shell_name!r} not found on PATH")


def _normalize_shebang(interp: list[str], program: Path) -> list[str]:
    if not interp:
        return [sys.executable]
    head = os.path.basename(interp[0]).lower()
    if head == "env" and len(interp) >= 2:
        cmd_idx = 1
        while cmd_idx < len(interp) and interp[cmd_idx].startswith("-"):
            cmd_idx += 1
        if cmd_idx >= len(interp):
            return interp
        target = os.path.basename(interp[cmd_idx]).lower()
        extra = interp[cmd_idx + 1 :]
    else:
        target = head
        extra = interp[1:]

    if target.startswith("python"):
        return [sys.executable, *extra]
    if target in ("sh", "bash", "dash", "zsh"):
        return [_resolve_shell(target, program), *extra]
    if os.name == "nt":
        resolved = shutil.which(target)
        if not resolved:
            raise CheckError(f"{program}: interpreter {target!r} from shebang not found on PATH")
        return [resolved, *extra]
    return interp


def invoke_check_reproducible(
    program: Path,
    metadata: dict[str, Any],
    *,
    layer: str | None,
    seed: int | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run once, or twice for layer=verify. Mismatched exit/stdout raises NonReproducibleError."""
    first = invoke_check(program, metadata, seed=seed, **kwargs)
    if layer != "verify":
        return first
    second = invoke_check(program, metadata, seed=seed, **kwargs)
    if first.returncode != second.returncode or (first.stdout or "") != (second.stdout or ""):
        raise NonReproducibleError(f"{program}: not reproducible")
    return first


def drop_ledger_fixture_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Product runs must not count ledger bad/good fixtures as hits."""
    return [hit for hit in hits if not is_ledger_fixture_path(str(hit.get("path") or ""))]


def config_file(root: Path) -> Path:
    path = config_path(root)
    if not path.is_file():
        raise CheckError(f"{path}: missing config.yaml")
    return path
