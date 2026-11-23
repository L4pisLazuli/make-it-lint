from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT

HOOKS = ROOT / "skills/make-it-lint/hooks"
WRAPPER = HOOKS / "stop-suggest.sh"
SESSION_LOG = HOOKS / "session_log.py"
STOP = HOOKS / "stop_suggest.py"


def run(cmd: list[str], payload: dict | str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(cmd, capture_output=True, text=True, input=text, env=env)


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / ".make-it-lint").mkdir()
        self.env = os.environ.copy()
        self.env.pop("MIL_SESSION_LOG", None)
        self.env["CLAUDE_PROJECT_DIR"] = str(self.project)

    def log(self, event: str, command: str, **extra) -> None:
        payload = {
            "session_id": "s1",
            "hook_event_name": event,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            **extra,
        }
        proc = run([sys.executable, str(SESSION_LOG)], payload, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def fail(self, command: str, error: str) -> None:
        self.log("PostToolUseFailure", command, error=error)

    def ok(self, command: str) -> None:
        self.log("PostToolUse", command, tool_response={"stdout": "", "stderr": ""})

    def stop(self, **extra) -> subprocess.CompletedProcess[str]:
        payload = {"session_id": "s1", "hook_event_name": "Stop", "stop_hook_active": False, **extra}
        proc = run([sys.executable, str(STOP)], payload, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_log_records_exit_code_and_first_error_line(self):
        self.fail("pytest tests/test_inventory.py", "Exit code 2\nERROR collecting tests/test_inventory.py\ntraceback")
        self.ok("pytest tests/test_inventory.py")
        rows = [
            json.loads(line)
            for line in (self.project / ".make-it-lint/cache/sessions/s1.ndjson")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(rows[0]["ok"], False)
        self.assertEqual(rows[0]["exit"], 2)
        self.assertEqual(rows[0]["error"], "ERROR collecting tests/test_inventory.py")
        self.assertEqual(rows[1]["ok"], True)

    def test_suggests_per_distinct_fixed_failure_and_matches_runner_family(self):
        # 1) Narrow pytest invocation fails, full pytest suite passes -> matches runner family!
        self.fail("pytest tests/test_totals.py -k q3_parser", "Exit code 1\nFAILED tests/test_totals.py::test_q3_parser")
        self.assertEqual(self.stop().stdout, "")
        self.ok("pytest -q")
        out = json.loads(self.stop().stdout)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertIn("`pytest tests/test_totals.py -k q3_parser` failed (exit 1)", context)
        self.assertIn("mil record", context)
        self.assertIn("without the user's yes", context)
        # Re-running stop without new failures stays quiet
        self.assertEqual(self.stop().stdout, "")

        # 2) Later in the SAME session, a second distinct failure is fixed -> suggested too!
        self.fail("ruff check src/pack_pick.py", "Exit code 1\nsrc/pack_pick.py:14:5: F821 Undefined name `sku_total`")
        self.ok("ruff check .")
        out2 = json.loads(self.stop().stdout)
        self.assertIn("ruff check src/pack_pick.py", out2["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.stop().stdout, "")

    def test_cursor_shell_and_stop_format(self):
        env = os.environ.copy()
        env.pop("MIL_SESSION_LOG", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        fail_payload = {
            "conversation_id": "cursor-conv-09",
            "hook_event_name": "postToolUseFailure",
            "tool_name": "Shell",
            "workspace_roots": [str(self.project)],
            "tool_input": {"command": "python3 -m unittest tests.test_totals"},
            "exit_code": 1,
            "error": "AssertionError: subtotal mismatch on BOLT-12",
        }
        ok_payload = {
            "conversation_id": "cursor-conv-09",
            "hook_event_name": "postToolUse",
            "tool_name": "Shell",
            "workspace_roots": [str(self.project)],
            "tool_input": {"command": "python3 -m unittest discover -s tests"},
            "exit_code": 0,
        }
        self.assertEqual(run([sys.executable, str(SESSION_LOG)], fail_payload, env).returncode, 0)
        self.assertEqual(run([sys.executable, str(SESSION_LOG)], ok_payload, env).returncode, 0)
        stop_proc = run(
            [sys.executable, str(STOP)],
            {
                "conversation_id": "cursor-conv-09",
                "hook_event_name": "stop",
                "workspace_roots": [str(self.project)],
                "loop_count": 0,
            },
            env,
        )
        self.assertEqual(stop_proc.returncode, 0)
        data = json.loads(stop_proc.stdout)
        self.assertIn("followup_message", data)
        self.assertIn("python3 -m unittest tests.test_totals", data["followup_message"])

    def test_quiet_for_answers_interrupts_and_active_stop(self):
        self.fail("grep -n WIDGET-04 inventory/items.yaml", "Exit code 1")
        self.ok("grep -n WIDGET-04 inventory/items.yaml")
        self.fail("cat inventory/items.yaml | rg BOLT-99", "Exit code 1")
        self.ok("cat inventory/items.yaml | rg BOLT-99")
        self.log("PostToolUseFailure", "sleep 100", error="Exit code 130", is_interrupt=True)
        self.ok("sleep 100")
        self.assertEqual(self.stop().stdout, "")
        self.fail("make build", "Exit code 2\nerror")
        self.ok("make build")
        self.assertEqual(self.stop(stop_hook_active=True).stdout, "")

    def test_ignores_bad_input_and_projects_without_ledger(self):
        proc = run([sys.executable, str(SESSION_LOG)], "not json", self.env)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        shutil.rmtree(self.project / ".make-it-lint")
        self.fail("make", "Exit code 2")
        self.assertFalse((self.project / ".make-it-lint").exists())
        self.assertEqual(self.stop().stdout, "")

    def test_wrapper_runs_the_python_hook(self):
        self.fail("npm test", "Exit code 1\nfail")
        self.ok("npm test")
        payload = {"session_id": "s1", "hook_event_name": "Stop", "stop_hook_active": False}
        proc = run(["sh", str(WRAPPER)], payload, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("additionalContext", proc.stdout)

    def test_cp932_stdio_and_broken_python3_stub_fallback(self):
        # Simulate Japanese Windows cp932 stdio with UTF-8 non-cp932 characters (`──▶`)
        cp932_env = dict(self.env, PYTHONIOENCODING="cp932")
        for event, extra in (
            ("PostToolUseFailure", {"error": "Exit code 1\n表示 ──▶ 構築 AssertionError"}),
            ("PostToolUse", {"tool_response": {"stdout": "", "stderr": ""}}),
        ):
            raw = json.dumps(
                {
                    "session_id": "cp932-session",
                    "hook_event_name": event,
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest tests/test_inventory.py"},
                    **extra,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            p = subprocess.run([sys.executable, str(SESSION_LOG)], input=raw, capture_output=True, env=cp932_env)
            self.assertEqual(p.returncode, 0)

        stop_raw = json.dumps(
            {"session_id": "cp932-session", "hook_event_name": "Stop", "stop_hook_active": False}
        ).encode("utf-8")
        stop_p = subprocess.run([sys.executable, str(STOP)], input=stop_raw, capture_output=True, env=cp932_env)
        self.assertEqual(stop_p.returncode, 0)
        stop_out = json.loads(stop_p.stdout.decode("utf-8"))
        self.assertIn("──▶", stop_out["hookSpecificOutput"]["additionalContext"])

        # Simulate WindowsApps/python3 stub exiting 49 while `python` points to real Python 3.10+
        if shutil.which("sh"):
            with tempfile.TemporaryDirectory() as bin_dir:
                stub = Path(bin_dir) / "python3"
                stub.write_text("#!/bin/sh\nexit 49\n", encoding="utf-8")
                stub.chmod(0o755)
                real_py = Path(bin_dir) / "python"
                real_py.symlink_to(sys.executable)
                stub_env = os.environ.copy()
                stub_env["PATH"] = bin_dir + os.pathsep + "/bin:/usr/bin"
                ver = subprocess.run(
                    ["sh", str(ROOT / "bin" / "mil"), "--version"],
                    capture_output=True,
                    text=True,
                    env=stub_env,
                )
                self.assertEqual(ver.returncode, 0, ver.stderr)
                self.assertEqual(ver.stdout.strip(), "make-it-lint/v1")


class LegacySessionLogTests(unittest.TestCase):
    def test_silent_without_log(self):
        env = os.environ.copy()
        env.pop("MIL_SESSION_LOG", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        proc = subprocess.run(["sh", str(WRAPPER)], capture_output=True, text=True, input="{}", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_suggests_once_when_session_log_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            log.write_text("pytest tests/test_totals.py exited 1\n", encoding="utf-8")
            env = os.environ.copy()
            env["MIL_SESSION_LOG"] = str(log)
            proc = subprocess.run(
                ["sh", str(WRAPPER)], capture_output=True, text=True, input="{}", env=env
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("additionalContext", proc.stdout)
            self.assertIn("mil record", proc.stdout)
            again = subprocess.run(
                ["sh", str(WRAPPER)], capture_output=True, text=True, input="{}", env=env
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(again.stdout, "")
