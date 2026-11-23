from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mil.human_ok import drop_agent_env
from tests.support import ROOT, pty, run_argv_tty

SETUP = ROOT / "bin" / "mil-setup"

BREAKPOINT_CHECK = '''#!/usr/bin/env python3
# ---
# id: MIL-BP-001
# incident: INCIDENT
# input: tree
# layer: lint
# tool: script
# ---
"""Fail when a Python file starts a line with breakpoint()."""
import sys
from pathlib import Path

args = sys.argv[1:]
root = Path(args[args.index("--root") + 1])
hit = False
for path in sorted(root.rglob("*.py")):
    if ".make-it-lint" in path.relative_to(root).parts:
        continue
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip().startswith("breakpoint()"):
            print(f"{path.relative_to(root).as_posix()}:{n}: remove breakpoint() before committing")
            hit = True
sys.exit(1 if hit else 0)
'''


def run_setup(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        ["sh", str(SETUP), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


@unittest.skipUnless(shutil.which("sh"), "needs sh")
class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_fresh_install_lints_clean(self):
        proc = run_setup(str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("mil lint: ok", proc.stdout)
        for rel in (
            "bin/mil",
            "bin/mil.cmd",
            "bin/mil-pre-commit",
            "lib/mil/__init__.py",
            "lib/mil/LICENSE",
            "schema/incident.schema.json",
            "docs/contract.md",
            ".claude/skills/make-it-lint/SKILL.md",
            ".claude/skills/make-it-lint/hooks/pre_tool_guard.py",
            ".cursor/skills/make-it-lint/SKILL.md",
            ".cursor/skills/make-it-lint/hooks/pre_tool_guard.py",
            ".make-it-lint/config.yaml",
            ".make-it-lint/waivers.yaml",
            ".github/workflows/make-it-lint.yml",
            ".github/scripts/pr-comment.py",
            ".gitattributes",
        ):
            self.assertTrue((self.tmp / rel).is_file(), rel)
        gitattrs = (self.tmp / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn(".make-it-lint/checks/** text eol=lf", gitattrs)
        gitignore = (self.tmp / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("__pycache__/", gitignore)
        self.assertIn("*.py[cod]", gitignore)
        self.assertEqual(list(self.tmp.rglob("__pycache__")), [])

    def test_no_claude_points_agents_md_to_cursor_skill(self):
        (self.tmp / "AGENTS.md").write_text("# Agent instructions\n", encoding="utf-8")
        proc = run_setup("--no-claude", str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        agents_text = (self.tmp / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(".cursor/skills/make-it-lint/SKILL.md", agents_text)
        self.assertNotIn(".claude/skills/", agents_text)

    def test_rerun_keeps_adopter_files_and_does_not_duplicate_lines(self):
        (self.tmp / ".gitignore").write_bytes(b"node_modules/\r\n.make-it-lint/cache/\r\n")
        (self.tmp / "LICENSE").write_text("Proprietary license\n", encoding="utf-8")
        (self.tmp / "AGENTS.md").write_text("# notes\n", encoding="utf-8")
        self.assertEqual(run_setup(str(self.tmp)).returncode, 0)

        config = self.tmp / ".make-it-lint" / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8") + "# local team note\n", encoding="utf-8")
        notes = self.tmp / ".make-it-lint" / "cache"
        notes.mkdir()
        (notes / "session.log").write_text("cached run\n", encoding="utf-8")
        before = {
            p: (self.tmp / p).read_bytes() for p in (".gitignore", ".gitattributes", "AGENTS.md")
        }

        proc = run_setup("--force", str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(config.read_text(encoding="utf-8").endswith("# local team note\n"))
        self.assertTrue((notes / "session.log").is_file())
        self.assertEqual((self.tmp / "LICENSE").read_text(encoding="utf-8"), "Proprietary license\n")
        for path, data in before.items():
            self.assertEqual((self.tmp / path).read_bytes(), data, path)
        ignore = (self.tmp / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(ignore.count(".make-it-lint/cache/"), 1)
        self.assertEqual((self.tmp / "AGENTS.md").read_text(encoding="utf-8").count("SKILL.md"), 1)

    def test_existing_workflow_needs_force(self):
        wf = self.tmp / ".github" / "workflows" / "make-it-lint.yml"
        wf.parent.mkdir(parents=True)
        wf.write_text("name: custom-workflow\n", encoding="utf-8")
        proc = run_setup(str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(wf.read_text(encoding="utf-8"), "name: custom-workflow\n")
        self.assertIn("--force to overwrite", proc.stdout)
        run_setup("--force", str(self.tmp))
        self.assertNotEqual(wf.read_text(encoding="utf-8"), "name: custom-workflow\n")

    def test_dry_run_writes_nothing(self):
        proc = run_setup("--dry-run", str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_hooks_register_claude_and_cursor_once_and_keep_existing_settings(self):
        self.git("init", "-q")
        settings = self.tmp / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(ls)"]},
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo existing-hook"}]}]},
                }
            ),
            encoding="utf-8",
        )
        for _ in range(2):
            proc = run_setup("--hooks", str(self.tmp))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})
        commands = [
            h["command"]
            for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop")
            for g in data["hooks"][event]
            for h in g["hooks"]
        ]
        self.assertIn("echo existing-hook", commands)
        self.assertEqual(sum("pre_tool_guard.py" in c for c in commands), 1)
        self.assertEqual(sum("session_log.py" in c for c in commands), 2)
        self.assertEqual(sum("stop_suggest.py" in c for c in commands), 1)

        cursor_hooks = json.loads((self.tmp / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cursor_hooks["hooks"]["preToolUse"]), 1)
        self.assertEqual(len(cursor_hooks["hooks"]["postToolUse"]), 1)
        self.assertEqual(len(cursor_hooks["hooks"]["stop"]), 1)

        hook = (self.tmp / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
        self.assertIn("mil-pre-commit", hook)

    def test_existing_git_hook_is_kept(self):
        self.git("init", "-q")
        hook = self.tmp / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho custom-pre-commit\n", encoding="utf-8")
        proc = run_setup("--hooks", str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(hook.read_text(encoding="utf-8"), "#!/bin/sh\necho custom-pre-commit\n")
        self.assertIn("bin/mil-pre-commit || exit", proc.stdout)

    def test_pre_commit_blocks_only_staged_block_hits(self):
        self.git("init", "-q")
        self.git("config", "user.email", "owner@example.com")
        self.git("config", "user.name", "ledger-owner")
        self.assertEqual(run_setup("--no-cursor", "--no-ci", str(self.tmp)).returncode, 0)
        work = self.tmp / "fixtures_tmp"
        (work / "bad" / "src").mkdir(parents=True)
        (work / "good" / "src").mkdir(parents=True)
        (work / "bad" / "src" / "worker.py").write_text("breakpoint()\n", encoding="utf-8")
        (work / "good" / "src" / "worker.py").write_text("def run():\n    return 0\n", encoding="utf-8")
        self.mil(
            "record",
            "--title",
            "Left breakpoint() in worker",
            "--fingerprint",
            "lint:breakpoint-left",
            "--source",
            "review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "breakpoint() was committed.",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "lint",
            "--layer-reason",
            "One file shows it.",
            "--bad",
            "fixtures_tmp/bad",
            "--good",
            "fixtures_tmp/good",
        )
        rid = next((self.tmp / ".make-it-lint" / "ledger").glob("F-*")).name
        check = self.tmp / ".make-it-lint" / "checks" / "no_breakpoint.py"
        check.write_text(BREAKPOINT_CHECK.replace("INCIDENT", rid), encoding="utf-8", newline="\n")
        check.chmod(0o755)
        self.mil("link", rid, ".make-it-lint/checks/no_breakpoint.py", "--message", "Remove breakpoint().")
        self.mil("prove", rid)
        for step in ("shadow", "comment"):
            self.assertEqual(self.mil_promote(step, rid, "--to", step, "--approved-by", "@ledger-owner").returncode, 0)
        # While temporary --bad fixtures_tmp/bad/src/worker.py is still in the product tree,
        # promoting to `block` refuses so the repo does not immediately break!
        premature = self.mil_promote("block", rid, "--to", "block", "--approved-by", "@ledger-owner")
        self.assertEqual(premature.returncode, 2)
        self.assertIn("fixtures_tmp/bad/src/worker.py", premature.stderr)
        shutil.rmtree(work)
        ok_block = self.mil_promote("block", rid, "--to", "block", "--approved-by", "@ledger-owner")
        self.assertEqual(ok_block.returncode, 0, ok_block.stdout + ok_block.stderr)
        self.git("add", "-A")
        self.git("update-index", "--chmod=+x", ".make-it-lint/checks/no_breakpoint.py")
        self.git("commit", "-q", "-m", "initial ledger and check")

        app = self.tmp / "src" / "worker.py"
        app.parent.mkdir()
        app.write_text("breakpoint()\n", encoding="utf-8")
        self.git("add", "src/worker.py")
        blocked = self.pre_commit()
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("src/worker.py:1", blocked.stdout)
        self.assertIn("--no-verify", blocked.stderr)

        app.write_text("def run():\n    return 0\n", encoding="utf-8")
        self.assertEqual(self.pre_commit().returncode, 1, "an unstaged fix must not count")
        self.git("add", "src/worker.py")
        passed = self.pre_commit()
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.tmp), *args], check=True, capture_output=True)

    def mil(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = drop_agent_env(os.environ)
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            ["sh", str(self.tmp / "bin" / "mil"), *args, "--root", str(self.tmp)],
            cwd=str(self.tmp),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"mil {args}\n{proc.stdout}{proc.stderr}")
        return proc

    def mil_promote(self, confirm_state: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = drop_agent_env(os.environ)
        env.pop("PYTHONPATH", None)
        if pty is None:
            import sys
            from tests.support import _WIN_TTY_RUNNER

            env["PYTHONPATH"] = str(self.tmp / "lib")
            return subprocess.run(
                [sys.executable, "-c", _WIN_TTY_RUNNER, f"{confirm_state}\n", "promote", *args, "--root", str(self.tmp)],
                cwd=str(self.tmp),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
        return run_argv_tty(
            f"{confirm_state}\n",
            ["sh", str(self.tmp / "bin" / "mil"), "promote", *args, "--root", str(self.tmp)],
            cwd=self.tmp,
            env=env,
        )

    def pre_commit(self) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            ["sh", str(self.tmp / "bin" / "mil-pre-commit")],
            cwd=str(self.tmp),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_refuses_kit_and_missing_target(self):
        self.assertEqual(run_setup(str(ROOT)).returncode, 2)
        self.assertEqual(run_setup(str(self.tmp / "missing")).returncode, 2)

    def test_python_module_installer_and_readme_quickstart_end_to_end(self):
        import sys

        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "owner@example.com")
        self.git("config", "user.name", "ledger-owner")

        # Invoke `python -m mil.setup_repo --hooks` directly (the Windows `bin\mil-setup.cmd` path)
        setup_env = os.environ.copy()
        setup_env["PYTHONPATH"] = str(ROOT / "lib")
        proc = subprocess.run(
            [sys.executable, "-m", "mil.setup_repo", "--hooks", str(self.tmp)],
            cwd=str(ROOT),
            env=setup_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("mil lint: ok", proc.stdout)
        self.assertFalse((self.tmp / "lib" / "mil" / "setup_repo.py").exists())

        # Simulate Windows checkout where `bin/mil` and `bin/mil-pre-commit` lack POSIX +x (0o644)
        (self.tmp / "bin" / "mil").chmod(0o644)
        (self.tmp / "bin" / "mil-pre-commit").chmod(0o644)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "chore: install make-it-lint")

        # Execute the exact README Quickstart CLI walkthrough (`record` -> `scaffold` -> edit -> `prove` -> `lint`)
        fx_tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, fx_tmp, True)
        (fx_tmp / "bad" / "src").mkdir(parents=True)
        (fx_tmp / "good" / "src").mkdir(parents=True)
        (fx_tmp / "bad" / "src" / "handler.py").write_text(
            "def handle(req):\n    breakpoint()\n    return req\n",
            encoding="utf-8",
        )
        (fx_tmp / "good" / "src" / "handler.py").write_text(
            "def handle(req):\n    return req\n",
            encoding="utf-8",
        )
        self.mil(
            "record",
            "--fingerprint",
            "lint:leftover-breakpoint",
            "--title",
            "Left breakpoint() in request handler",
            "--what",
            "Agent committed a debug breakpoint() call in src/handler.py.",
            "--source",
            "PR review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "@ledger-owner",
            "--bad",
            str(fx_tmp / "bad"),
            "--good",
            str(fx_tmp / "good"),
            "--auto",
        )
        shutil.rmtree(fx_tmp, ignore_errors=True)

        rid = sorted(p.name for p in (self.tmp / ".make-it-lint" / "ledger").glob("F-*"))[-1]
        scaf = self.mil("scaffold", rid, "--id", "MIL-BP-001", "--path", ".make-it-lint/checks/no_breakpoint.py")
        self.assertIn("drafted provisional check", scaf.stdout)
        self.assertIn("strategy=py_ast_call", scaf.stdout)

        check_file = self.tmp / ".make-it-lint" / "checks" / "no_breakpoint.py"
        scaffold_text = check_file.read_text(encoding="utf-8")
        self.assertIn("forbidden_calls", scaffold_text)

        self.mil("prove", rid)
        self.mil("lint")
        for step in ("shadow", "comment", "block"):
            self.assertEqual(self.mil_promote(step, rid, "--to", step, "--approved-by", "@ledger-owner").returncode, 0)
        self.git("add", "-A")
        self.git("update-index", "--chmod=+x", ".make-it-lint/checks/no_breakpoint.py")
        self.git("commit", "-q", "-m", "feat: add MIL-BP-001 check")

        # Verify `.git/hooks/pre-commit` works even with `bin/mil-pre-commit` at mode 0o644!
        (self.tmp / "src").mkdir(exist_ok=True)
        (self.tmp / "src" / "handler.py").write_text("def handle(req):\n    breakpoint()\n    return req\n", encoding="utf-8")
        self.git("add", "src/handler.py")
        hook_proc = subprocess.run(
            ["sh", str(self.tmp / ".git" / "hooks" / "pre-commit")],
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
        )
        self.assertEqual(hook_proc.returncode, 1, hook_proc.stdout + hook_proc.stderr)
        self.assertIn("src/handler.py:2", hook_proc.stdout)
