"""Install or refresh make-it-lint in an adopter repository (`mil-setup`)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

USAGE = """\
usage: mil-setup [options] TARGET

Install make-it-lint (bin/mil, bin/mil.cmd, lib/mil, schema, skill,
ledger skeleton, CI workflow) into the repository at TARGET.

options:
  --no-claude   do not install the skill into .claude/skills/
  --no-cursor   do not install the skill into .cursor/skills/
  --no-ci       do not install .github/workflows/make-it-lint.yml
                and .github/scripts/pr-comment.py
  --hooks       also install the git pre-commit hook (runs shadow /
                comment / block checks on staged files) and the
                Claude Code / Cursor hooks (PreToolUse guard, session
                log, and Stop suggestion)
  --force       overwrite an existing CI workflow and comment script
                (.make-it-lint/ is never overwritten)
  --dry-run     print what would change and change nothing
  -h, --help    show this help

Re-running is safe: kit files are refreshed, your ledger, config,
waivers, and checks are kept.
"""

REQUIRED_KIT_PATHS = (
    "bin/mil",
    "bin/mil.cmd",
    "lib/mil/__init__.py",
    "schema",
    "docs/contract.md",
    "template/.make-it-lint",
    "skills/make-it-lint/SKILL.md",
    ".github/scripts/pr-comment.py",
)

HOOK_FILES = (
    "pre_tool_guard.py",
    "session_log.py",
    "stop_suggest.py",
    "stop-suggest.sh",
)

NEXT_STEPS = """
Next:
  1. Commit the installed files:
       git add -A && git commit -m "chore: install make-it-lint"
  2. When an agent makes a mistake:
     - In Claude Code or Cursor: run /make-it-lint (or ask it to turn the
       mistake into a check).
     - From the CLI: bin/mil record ... -> bin/mil scaffold F-... -> bin/mil prove F-...
  3. Enable a proven check in your own terminal:
       bin/mil promote F-... --approved-by @you
"""


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if callable(reconf) and enc != "utf8":
            try:
                reconf(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _note(action: str, detail: str) -> None:
    print(f"{action:<8} {detail}")


def _make_exec(path: Path, *, dry: bool) -> None:
    if dry or not path.is_file():
        return
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _find_hook_python_cmd() -> str | None:
    candidates: list[tuple[list[str], str]] = [
        (["python3"], "python3"),
        (["python"], "python"),
    ]
    if os.name == "nt":
        candidates.insert(0, (["py", "-3"], "py -3"))
    for argv, rendered in candidates:
        exe = shutil.which(argv[0])
        if not exe:
            continue
        try:
            proc = subprocess.run(
                [*argv, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return rendered
    return None


def _kit_file(kit: Path, target: Path, src_rel: str, dst_rel: str, *, dry: bool) -> None:
    src = kit / src_rel
    dst = target / dst_rel
    if dst.exists():
        if dst.is_file() and src.read_bytes() == dst.read_bytes():
            _note("same", dst_rel)
            return
        _note("update", dst_rel)
    else:
        _note("add", dst_rel)
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _kit_dir(
    kit: Path,
    target: Path,
    src_rel: str,
    dst_rel: str,
    *,
    dry: bool,
    extra_ignore: tuple[str, ...] = (),
) -> None:
    src = kit / src_rel
    dst = target / dst_rel
    if dst.exists():
        _note("update", f"{dst_rel}/")
    else:
        _note("add", f"{dst_rel}/")
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.pyd", *extra_ignore)
    shutil.copytree(src, dst, ignore=ignore)


def _own_file(
    kit: Path,
    target: Path,
    src_rel: str,
    dst_rel: str,
    *,
    dry: bool,
    force: bool,
    never_overwrite: bool = False,
) -> None:
    src = kit / src_rel
    dst = target / dst_rel
    if dst.exists():
        if dst.is_file() and src.read_bytes() == dst.read_bytes():
            _note("same", dst_rel)
            return
        if never_overwrite:
            _note("keep", dst_rel)
            return
        if not force:
            _note("keep", f"{dst_rel} (differs from the kit; --force to overwrite)")
            return
        _note("replace", dst_rel)
    else:
        _note("add", dst_rel)
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _append_lines(kit: Path, target: Path, src_rel: str, dst_rel: str, header: str, *, dry: bool) -> None:
    src = kit / src_rel
    dst = target / dst_rel
    src_lines = src.read_text(encoding="utf-8").replace("\r", "").splitlines()
    existing_lines: set[str] = set()
    dst_bytes = b""
    if dst.is_file():
        dst_bytes = dst.read_bytes()
        existing_lines = set(dst_bytes.decode("utf-8", errors="replace").replace("\r", "").splitlines())

    missing: list[str] = []
    for line in src_lines:
        if not line or line.startswith("#"):
            continue
        if line not in existing_lines and line not in missing:
            missing.append(line)

    if not missing:
        _note("same", dst_rel)
        return
    _note("append" if dst.is_file() else "add", dst_rel)
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    sep = b""
    if dst_bytes:
        sep = b"\n" if dst_bytes.endswith(b"\n") else b"\n\n"
    payload = (header + "\n" + "\n".join(missing) + "\n").encode("utf-8")
    with dst.open("ab") as fh:
        fh.write(sep + payload)


def _install_git_hook(target: Path, *, dry: bool) -> None:
    git = shutil.which("git")
    if not git:
        _note("skip", "git pre-commit hook (git not found on PATH)")
        return
    proc = subprocess.run(
        [git, "-C", str(target), "rev-parse", "--git-path", "hooks"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        _note("skip", f"git pre-commit hook ({target} is not a git repository)")
        return
    raw_hookdir = proc.stdout.strip()
    hook_path_candidate = Path(raw_hookdir)
    if hook_path_candidate.is_absolute() or (len(raw_hookdir) >= 2 and raw_hookdir[1] == ":"):
        hookdir = hook_path_candidate
    else:
        hookdir = target / raw_hookdir
    hook = hookdir / "pre-commit"
    if hook.is_file():
        body = hook.read_text(encoding="utf-8", errors="replace")
        if "mil-pre-commit" in body:
            _note("same", "git pre-commit hook")
        else:
            _note("keep", "git pre-commit hook (yours; add this line to it: sh bin/mil-pre-commit || exit $?)")
        return
    _note("add", "git pre-commit hook -> bin/mil-pre-commit")
    if dry:
        return
    hookdir.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(
        b"#!/bin/sh\n"
        b"# make-it-lint pre-commit hook, installed by mil-setup --hooks.\n"
        b'exec sh "$(git rev-parse --show-toplevel)/bin/mil-pre-commit"\n'
    )
    _make_exec(hook, dry=False)


def _install_claude_hooks(target: Path, *, claude: bool, dry: bool, pycmd: str | None) -> None:
    if not claude:
        _note("skip", "Claude Code hooks (--no-claude)")
        return
    if not pycmd:
        _note("skip", "Claude Code hooks (Python 3.10+ not found)")
        return
    path = target / ".claude" / "settings.json"
    rel = ".claude/settings.json"
    hooks_dir = '"$CLAUDE_PROJECT_DIR/.claude/skills/make-it-lint/hooks'
    wanted = [
        ("PreToolUse", None, f'{pycmd} {hooks_dir}/pre_tool_guard.py"'),
        ("PostToolUse", "Bash", f'{pycmd} {hooks_dir}/session_log.py"'),
        ("PostToolUseFailure", "Bash", f'{pycmd} {hooks_dir}/session_log.py"'),
        ("Stop", None, f'{pycmd} {hooks_dir}/stop_suggest.py"'),
    ]
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            _note("warn", f"{rel} is not valid JSON; hooks not registered")
            return
        if not isinstance(loaded, dict):
            _note("warn", f"{rel} is not a JSON object; hooks not registered")
            return
        data = loaded
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        _note("warn", f"{rel} hooks field is not an object; hooks not registered")
        return
    added: list[str] = []
    for event, matcher, command in wanted:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            continue
        script_name = command.split("/")[-1].rstrip('"')
        aliases = {script_name}
        if script_name == "stop_suggest.py":
            aliases.add("stop-suggest.sh")
        present = any(
            "make-it-lint/hooks/" in str(h.get("command", ""))
            and any(a in str(h.get("command", "")) for a in aliases)
            for g in groups
            if isinstance(g, dict)
            for h in (g.get("hooks") or [])
            if isinstance(h, dict)
        )
        if present:
            continue
        group: dict = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
        if matcher:
            group = {"matcher": matcher, **group}
        groups.append(group)
        added.append(event)
    if not added:
        _note("same", f"{rel} (make-it-lint hooks)")
        return
    _note("append" if path.exists() else "add", f"{rel} hooks: {', '.join(added)}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _install_cursor_hooks(target: Path, *, cursor: bool, dry: bool, pycmd: str | None) -> None:
    if not cursor:
        _note("skip", "Cursor hooks (--no-cursor)")
        return
    if not pycmd:
        _note("skip", "Cursor hooks (Python 3.10+ not found)")
        return
    path = target / ".cursor" / "hooks.json"
    rel = ".cursor/hooks.json"
    hooks_dir = ".cursor/skills/make-it-lint/hooks"
    wanted = [
        ("preToolUse", None, f"{pycmd} {hooks_dir}/pre_tool_guard.py"),
        ("postToolUse", "Shell", f"{pycmd} {hooks_dir}/session_log.py"),
        ("postToolUseFailure", "Shell", f"{pycmd} {hooks_dir}/session_log.py"),
        ("stop", None, f"{pycmd} {hooks_dir}/stop_suggest.py"),
    ]
    data: dict = {"version": 1, "hooks": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            _note("warn", f"{rel} is not valid JSON; hooks not registered")
            return
        if not isinstance(loaded, dict):
            _note("warn", f"{rel} is not a JSON object; hooks not registered")
            return
        data = loaded
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        _note("warn", f"{rel} hooks field is not an object; hooks not registered")
        return
    added: list[str] = []
    for event, matcher, command in wanted:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            continue
        script_name = command.split("/")[-1]
        aliases = {script_name}
        if script_name == "stop_suggest.py":
            aliases.add("stop-suggest.sh")
        present = any(
            isinstance(e, dict)
            and "make-it-lint/hooks/" in str(e.get("command", ""))
            and any(a in str(e.get("command", "")) for a in aliases)
            for e in entries
        )
        if present:
            continue
        entry: dict = {"command": command, "timeout": 10}
        if matcher:
            entry["matcher"] = matcher
        entries.append(entry)
        added.append(event)
    if not added:
        _note("same", f"{rel} (make-it-lint hooks)")
        return
    _note("append" if path.exists() else "add", f"{rel} hooks: {', '.join(added)}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    raw_args = list(sys.argv[1:] if argv is None else argv)

    claude = True
    cursor = True
    ci = True
    hooks = False
    force = False
    dry = False
    target_raw: str | None = None

    i = 0
    while i < len(raw_args):
        arg = raw_args[i]
        if arg == "--no-claude":
            claude = False
        elif arg == "--no-cursor":
            cursor = False
        elif arg == "--no-ci":
            ci = False
        elif arg == "--hooks":
            hooks = True
        elif arg == "--force":
            force = True
        elif arg == "--dry-run":
            dry = True
        elif arg in ("-h", "--help"):
            print(USAGE, end="")
            return 0
        elif arg == "--":
            i += 1
            break
        elif arg.startswith("-"):
            print(f"mil-setup: unknown option: {arg}", file=sys.stderr)
            print(USAGE, end="", file=sys.stderr)
            return 2
        else:
            if target_raw is not None:
                print("mil-setup: only one TARGET is allowed", file=sys.stderr)
                return 2
            target_raw = arg
        i += 1

    if target_raw is None and i < len(raw_args):
        if len(raw_args) - i > 1:
            print("mil-setup: only one TARGET is allowed", file=sys.stderr)
            return 2
        target_raw = raw_args[i]

    if not target_raw:
        print(USAGE, end="", file=sys.stderr)
        return 2

    kit = Path(__file__).resolve().parents[2]
    target_candidate = Path(target_raw)
    if not target_candidate.is_dir():
        print(f"mil-setup: {target_raw}: not a directory", file=sys.stderr)
        return 2
    target = target_candidate.resolve()
    if target == kit:
        print(f"mil-setup: {target}: this is the kit itself; pass another repository", file=sys.stderr)
        return 2

    for need in REQUIRED_KIT_PATHS:
        if not (kit / need).exists():
            print(f"mil-setup: {need}: missing from the kit at {kit}", file=sys.stderr)
            return 2

    if dry:
        print("mil-setup: dry run, nothing is written")
    print(f"mil-setup: {kit} -> {target}")

    # CLI & library
    _kit_file(kit, target, "bin/mil", "bin/mil", dry=dry)
    _make_exec(target / "bin" / "mil", dry=dry)
    _kit_file(kit, target, "bin/mil.cmd", "bin/mil.cmd", dry=dry)
    _kit_file(kit, target, "bin/mil-pre-commit", "bin/mil-pre-commit", dry=dry)
    _make_exec(target / "bin" / "mil-pre-commit", dry=dry)
    _kit_dir(kit, target, "lib/mil", "lib/mil", dry=dry, extra_ignore=("setup_repo.py",))
    if not dry:
        shutil.copyfile(kit / "LICENSE", target / "lib" / "mil" / "LICENSE")
    _kit_dir(kit, target, "schema", "schema", dry=dry)
    _kit_file(kit, target, "docs/contract.md", "docs/contract.md", dry=dry)

    target_license = target / "LICENSE"
    if not target_license.exists() or (
        target_license.is_file() and target_license.read_bytes() == (kit / "LICENSE").read_bytes()
    ):
        _own_file(kit, target, "LICENSE", "LICENSE", dry=dry, force=force, never_overwrite=True)
    else:
        _note("keep", "LICENSE (yours; the kit's MIT license is in lib/mil/LICENSE)")

    # Agent skills
    if claude:
        _kit_dir(kit, target, "skills/make-it-lint", ".claude/skills/make-it-lint", dry=dry)
        for hf in HOOK_FILES:
            _make_exec(target / ".claude" / "skills" / "make-it-lint" / "hooks" / hf, dry=dry)
    if cursor:
        _kit_dir(kit, target, "skills/make-it-lint", ".cursor/skills/make-it-lint", dry=dry)
        for hf in HOOK_FILES:
            _make_exec(target / ".cursor" / "skills" / "make-it-lint" / "hooks" / hf, dry=dry)

    # Ledger skeleton
    for rel_item in ("config.yaml", "waivers.yaml", "ledger/.gitkeep", "checks/.gitkeep"):
        _own_file(
            kit,
            target,
            f"template/.make-it-lint/{rel_item}",
            f".make-it-lint/{rel_item}",
            dry=dry,
            force=force,
            never_overwrite=True,
        )

    target_cfg = target / ".make-it-lint" / "config.yaml"
    if target_cfg.is_file():
        cfg_lines = target_cfg.read_text(encoding="utf-8", errors="replace").splitlines()
        if not any(ln.startswith("contract:") for ln in cfg_lines):
            _note(
                "warn",
                ".make-it-lint/config.yaml has no contract: line; copy the contract and ledger sections from template/.make-it-lint/config.yaml",
            )

    # CI workflow
    if ci:
        _own_file(
            kit,
            target,
            "template/.github/workflows/make-it-lint.yml",
            ".github/workflows/make-it-lint.yml",
            dry=dry,
            force=force,
        )
        _own_file(
            kit,
            target,
            ".github/scripts/pr-comment.py",
            ".github/scripts/pr-comment.py",
            dry=dry,
            force=force,
        )

    # Optional hooks
    if hooks:
        pycmd = _find_hook_python_cmd()
        _install_git_hook(target, dry=dry)
        _install_claude_hooks(target, claude=claude, dry=dry, pycmd=pycmd)
        _install_cursor_hooks(target, cursor=cursor, dry=dry, pycmd=pycmd)

    _append_lines(kit, target, "template/.gitignore", ".gitignore", "# make-it-lint", dry=dry)
    _append_lines(kit, target, "template/.gitattributes", ".gitattributes", "# make-it-lint", dry=dry)

    skill_path = ""
    if claude:
        skill_path = ".claude/skills/make-it-lint/SKILL.md"
    elif cursor:
        skill_path = ".cursor/skills/make-it-lint/SKILL.md"
    if skill_path:
        pointer = f"When an agent mistake is reported, follow {skill_path}."
        agents_md = target / "AGENTS.md"
        if agents_md.is_file():
            text = agents_md.read_text(encoding="utf-8", errors="replace")
            if "make-it-lint/SKILL.md" in text:
                _note("same", "AGENTS.md")
            else:
                _note("append", "AGENTS.md (skill pointer)")
                if not dry:
                    with agents_md.open("ab") as fh:
                        fh.write(f"\n{pointer}\n".encode("utf-8"))
        else:
            _note("skip", f"AGENTS.md (none; Codex and similar agents need: {pointer})")

    if dry:
        return 0

    print()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target / "lib")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    lint_proc = subprocess.run(
        [sys.executable, "-m", "mil", "lint", "--root", str(target)],
        env=env,
        stdin=subprocess.DEVNULL,
    )
    if lint_proc.returncode != 0:
        return lint_proc.returncode

    print(NEXT_STEPS, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
