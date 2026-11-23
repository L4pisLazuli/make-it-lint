"""Shared paths and mil invocation for tests."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import pty
except ImportError:  # pragma: no cover - Windows stdlib has no pty
    pty = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from mil.human_ok import drop_agent_env  # noqa: E402

MIL = ROOT / "bin" / "mil"
SAMPLE = ROOT / "examples" / "sample-repo"
TEMPLATE = ROOT / "template"

_WIN_TTY_RUNNER = """\
import io, sys
from mil.cli import main

class _TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True

sys.stdin = _TtyInput(sys.argv[1])
sys.argv = ["mil", *sys.argv[2:]]
raise SystemExit(main())
"""


def mil_env(env: dict[str, str] | None = None) -> dict[str, str]:
    merged = drop_agent_env(os.environ)
    merged["PYTHONPATH"] = str(LIB) + os.pathsep + merged.get("PYTHONPATH", "")
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
    # Pin the date so record IDs and expiry checks do not depend on the clock.
    merged.setdefault("MIL_TODAY", "2026-11-16")
    if env:
        merged.update(env)
    return merged


def _mil_cmd(*args: str) -> list[str]:
    if os.name == "nt":
        return [sys.executable, "-m", "mil", *map(str, args)]
    return [str(MIL), *map(str, args)]


def run_mil(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _mil_cmd(*args),
        cwd=str(cwd or ROOT),
        env=mil_env(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def run_mil_tty(
    confirm_line: str,
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `mil` connected to a terminal stdin and feed `confirm_line`."""
    merged = mil_env(env)
    if pty is None:
        cmd = [sys.executable, "-c", _WIN_TTY_RUNNER, confirm_line, *map(str, args)]
        return subprocess.run(
            cmd,
            cwd=str(cwd or ROOT),
            env=merged,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    return run_argv_tty(confirm_line, _mil_cmd(*args), cwd=cwd or ROOT, env=merged)


def run_argv_tty(
    confirm_line: str,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run arbitrary `argv` with stdin attached to a pseudo-terminal."""
    if pty is None:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            input=confirm_line,
            capture_output=True,
            text=True,
        )
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        os.close(slave)
        slave = -1
        payload = confirm_line.encode("utf-8")
        if not confirm_line.endswith("\n"):
            # Send VEOF (Ctrl-D) so canonical TTY readline() returns without hanging.
            payload += b"\x04\x04" if payload else b"\x04"
        os.write(master, payload)
        out_b, err_b = proc.communicate(timeout=30)
        return subprocess.CompletedProcess(
            argv,
            proc.returncode,
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"),
        )
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)


def copy_tree(src: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="mil-"))
    shutil.copytree(src, dest / src.name, dirs_exist_ok=True)
    return dest / src.name


def scratch_adopter() -> Path:
    dest = Path(tempfile.mkdtemp(prefix="mil-adopter-"))
    shutil.copytree(TEMPLATE / ".make-it-lint", dest / ".make-it-lint")
    return dest


PATH_FLAGS = {"--bad", "--good", "--from-ci", "--check", "--base", "--head", "--json-summary", "--events"}


def parse_help_examples(text: str) -> list[list[str]]:
    """Command argv lists from an argparse epilog `examples:` block."""
    commands: list[str] = []
    buf: list[str] = []
    in_examples = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not in_examples:
            if stripped.lower().startswith("examples:"):
                in_examples = True
            continue
        if not stripped:
            if buf:
                commands.append(" ".join(buf))
                buf = []
            continue
        if stripped.endswith("\\"):
            buf.append(stripped[:-1].rstrip())
            continue
        buf.append(stripped)
        commands.append(" ".join(buf))
        buf = []
    if buf:
        commands.append(" ".join(buf))
    out: list[list[str]] = []
    for cmd in commands:
        argv = shlex.split(cmd)
        if not argv:
            continue
        if argv[0] in {"mil", "bin/mil"}:
            argv = argv[1:]
        out.append(argv)
    return out


def materialize_help_paths(cwd: Path, argv: list[str]) -> None:
    """Create local files named by help-example path flags, with realistic contents."""
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag in PATH_FLAGS and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            target = Path(argv[i + 1])
            if not target.is_absolute():
                target = cwd / target
            target.parent.mkdir(parents=True, exist_ok=True)
            if flag in {"--base", "--head"}:
                target.mkdir(parents=True, exist_ok=True)
            elif flag == "--from-ci":
                target.write_text(
                    "tests/order_totals.py:41: AssertionError: parser output drifted from the stored Q3 baseline\n",
                    encoding="utf-8",
                )
            elif flag == "--good":
                target.write_text(
                    "# fixed collect PR: inventory/ only; config/settings.yaml is not in this tree\n"
                    "inventory:\n"
                    "  - sku: WIDGET-04\n"
                    "    qty: 12\n",
                    encoding="utf-8",
                )
            elif flag == "--json-summary":
                pass
            else:
                target.write_text(
                    "# collect PR that also edited config/settings.yaml\n"
                    "timeout_seconds: 30\n"
                    "collect:\n"
                    "  allow:\n"
                    "    - inventory/\n",
                    encoding="utf-8",
                )
            i += 2
            continue
        i += 1
