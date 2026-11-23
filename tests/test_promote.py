from __future__ import annotations

import subprocess
import unittest

from mil.ledger import load_record_file
from mil.models import transitions_ok
from tests.support import SAMPLE, copy_tree, run_mil, run_mil_tty

PROVEN = "F-20260402-01"
SHADOW = "F-20260311-01"
COMMENT = "F-20260609-01"
OPEN = "F-20260722-01"


def record(root, rid):
    return load_record_file(root / ".make-it-lint" / "ledger" / rid / "record.yaml")


class PromoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = copy_tree(SAMPLE)

    def promote_tty(self, confirm: str, *args: str, env: dict[str, str] | None = None):
        return run_mil_tty(confirm, "promote", *args, "--root", str(self.root), env=env)

    def promote_pipe(self, *args: str, env: dict[str, str] | None = None):
        return run_mil("promote", *args, "--root", str(self.root), env=env)

    def test_proven_to_shadow_records_who_approved(self):
        proc = self.promote_tty("shadow\n", PROVEN, "--approved-by", "@ledger-owner", "--note", "asked after prove")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("proven -> shadow", proc.stdout)
        rec = record(self.root, PROVEN)
        self.assertEqual(rec["enforcement"]["state"], "shadow")
        last = rec["history"][-1]
        self.assertEqual(last["state"], "shadow")
        self.assertEqual(last["from_state"], "proven")
        self.assertEqual(last["approved_by"], "@ledger-owner")
        self.assertEqual(last["note"], "asked after prove")
        self.assertIsNone(transitions_ok([h["state"] for h in rec["history"]]))
        lint = run_mil("lint", "--root", str(self.root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_refuses_non_tty_agent_runtime_and_wrong_confirmation(self):
        non_tty = self.promote_pipe(PROVEN, "--approved-by", "@owner")
        self.assertEqual(non_tty.returncode, 2)
        self.assertIn("stdin is not a terminal", non_tty.stderr)
        self.assertEqual(record(self.root, PROVEN)["enforcement"]["state"], "proven")

        for var in ("CURSOR_AGENT", "CLAUDECODE", "CODEX_SANDBOX"):
            agent_env = self.promote_tty("shadow\n", PROVEN, "--approved-by", "@owner", env={var: "1"})
            self.assertEqual(agent_env.returncode, 2)
            self.assertIn(f"refusing to run in an agent environment ({var})", agent_env.stderr)
            self.assertEqual(record(self.root, PROVEN)["enforcement"]["state"], "proven")

        wrong_word = self.promote_tty("yes\n", PROVEN, "--approved-by", "@owner")
        self.assertEqual(wrong_word.returncode, 2)
        self.assertIn("confirmation did not match: type shadow exactly", wrong_word.stderr)
        self.assertEqual(record(self.root, PROVEN)["enforcement"]["state"], "proven")

        eof_tty = self.promote_tty("", PROVEN, "--approved-by", "@owner")
        self.assertEqual(eof_tty.returncode, 2)
        self.assertIn("confirmation did not match: type shadow exactly", eof_tty.stderr)

    def test_refuses_agent_as_approver(self):
        proc = self.promote_tty("shadow\n", PROVEN, "--approved-by", "cloud-agent")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--approved-by looks like an agent", proc.stderr)

    def test_one_step_at_a_time(self):
        proc = self.promote_tty("block\n", PROVEN, "--to", "block", "--approved-by", "@owner")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("skips a state", proc.stderr)
        self.assertIn("--to shadow", proc.stderr)
        self.assertEqual(record(self.root, PROVEN)["enforcement"]["state"], "proven")

        self.assertEqual(self.promote_tty("comment\n", SHADOW, "--approved-by", "@owner").returncode, 0)
        self.assertEqual(record(self.root, SHADOW)["enforcement"]["state"], "comment")
        self.assertEqual(self.promote_tty("block\n", COMMENT, "--to", "block", "--approved-by", "@owner").returncode, 0)
        self.assertEqual(record(self.root, COMMENT)["enforcement"]["state"], "block")
        again = self.promote_tty("block\n", COMMENT, "--approved-by", "@owner")
        self.assertEqual(again.returncode, 2)
        self.assertIn("already at block", again.stderr)

    def test_block_gate_refuses_red_tree_check_and_passes_once_cleaned(self):
        # F-20260518-01 (MIL-SUITE-001, input: tree) scans tools/run_suite.py for sys.exit(0) after failures.
        suite_id = "F-20260518-01"
        for step in ("shadow", "comment"):
            self.assertEqual(self.promote_tty(f"{step}\n", suite_id, "--approved-by", "@owner").returncode, 0)

        bad_runner = (self.root / ".make-it-lint" / "ledger" / suite_id / "bad" / "run_suite.py").read_text(
            encoding="utf-8"
        )
        tools_runner = self.root / "tools" / "run_suite.py"
        tools_runner.parent.mkdir(parents=True, exist_ok=True)
        tools_runner.write_text(bad_runner, encoding="utf-8")

        dry = self.promote_pipe(suite_id, "--to", "block", "--approved-by", "@owner", "--dry-run")
        self.assertEqual(dry.returncode, 2)
        self.assertIn("the check is red on the current tree", dry.stderr)

        live = self.promote_tty("block\n", suite_id, "--to", "block", "--approved-by", "@owner")
        self.assertEqual(live.returncode, 2)
        self.assertIn("tools/run_suite.py", live.stderr)
        self.assertEqual(record(self.root, suite_id)["enforcement"]["state"], "comment")

        good_runner = (self.root / ".make-it-lint" / "ledger" / suite_id / "good" / "run_suite.py").read_text(
            encoding="utf-8"
        )
        tools_runner.write_text(good_runner, encoding="utf-8")
        promoted = self.promote_tty("block\n", suite_id, "--to", "block", "--approved-by", "@owner")
        self.assertEqual(promoted.returncode, 0, promoted.stderr)
        self.assertEqual(record(self.root, suite_id)["enforcement"]["state"], "block")

    def test_block_gate_refuses_red_git_diff_check_on_staged_index(self):
        subprocess.run(["git", "init", "-q", "-b", "collect/q4-warehouse"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "owner@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "ledger-owner"], cwd=self.root, check=True)
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "runner.yaml").write_text("timeout_seconds: 30\n", encoding="utf-8")
        (self.root / "inventory").mkdir(exist_ok=True)
        (self.root / "inventory" / "items.yaml").write_text("sku: WIDGET-04\nqty: 12\n", encoding="utf-8")
        # Clear the sample waiver on config/runner.yaml so unauthorized staged edits block.
        (self.root / ".make-it-lint" / "waivers.yaml").write_text("[]\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=self.root, check=True)

        # Move SHADOW (F-20260311-01, MIL-SCOPE-001, input: diff) -> comment
        self.assertEqual(self.promote_tty("comment\n", SHADOW, "--approved-by", "@owner").returncode, 0)

        # Stage an out-of-scope edit on a collect/* branch -> promote to block must refuse
        (self.root / "config" / "runner.yaml").write_text("timeout_seconds: 90\n", encoding="utf-8")
        subprocess.run(["git", "add", "config/runner.yaml"], cwd=self.root, check=True)
        red = self.promote_tty("block\n", SHADOW, "--to", "block", "--approved-by", "@owner")
        self.assertEqual(red.returncode, 2)
        self.assertIn("config/runner.yaml", red.stderr)
        self.assertEqual(record(self.root, SHADOW)["enforcement"]["state"], "comment")

        # Revert unauthorized staged change and stage an allowed inventory change -> promote succeeds
        subprocess.run(["git", "checkout", "HEAD", "--", "config/runner.yaml"], cwd=self.root, check=True)
        (self.root / "inventory" / "items.yaml").write_text("sku: WIDGET-04\nqty: 18\n", encoding="utf-8")
        subprocess.run(["git", "add", "inventory/items.yaml"], cwd=self.root, check=True)
        green = self.promote_tty("block\n", SHADOW, "--to", "block", "--approved-by", "@owner")
        self.assertEqual(green.returncode, 0, green.stderr)
        self.assertEqual(record(self.root, SHADOW)["enforcement"]["state"], "block")

    def test_refuses_unproven_and_needs_approver(self):
        proc = self.promote_tty("shadow\n", OPEN, "--approved-by", "@owner")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("run mil prove first", proc.stderr)

        missing = self.promote_pipe(PROVEN)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--approved-by", missing.stderr)
        empty = self.promote_tty("shadow\n", PROVEN, "--approved-by", "  ")
        self.assertEqual(empty.returncode, 2)

        path = self.root / ".make-it-lint" / "ledger" / PROVEN / "record.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("last_result: red-on-bad, green-on-good", "last_result: green-on-bad, green-on-good"),
            encoding="utf-8",
        )
        stale = self.promote_tty("shadow\n", PROVEN, "--approved-by", "@owner")
        self.assertEqual(stale.returncode, 2)
        self.assertIn("proof.last_result", stale.stderr)

    def test_dry_run_writes_nothing_without_tty(self):
        path = self.root / ".make-it-lint" / "ledger" / PROVEN / "record.yaml"
        before = path.read_bytes()
        proc = self.promote_pipe(PROVEN, "--approved-by", "@owner", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would promote", proc.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_record(self):
        proc = self.promote_tty("shadow\n", "F-20990101-01", "--approved-by", "@owner")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no such ledger record", proc.stderr)
