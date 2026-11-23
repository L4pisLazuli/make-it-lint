from __future__ import annotations

import os
import subprocess
import unittest

from mil.ledger import load_record_file, save_record_file
from tests.support import SAMPLE, copy_tree, run_mil, scratch_adopter


class LintInvariantTests(unittest.TestCase):
    def test_skipped_rollout_state_is_rejected(self):
        repo = copy_tree(SAMPLE)
        rec = repo / ".make-it-lint/ledger/F-20260609-01/record.yaml"
        data = load_record_file(rec)
        data["history"] = [h for h in data["history"] if h.get("state") != "shadow"]
        save_record_file(rec, data)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("illegal transition", proc.stdout)

    def test_check_without_incident_or_origin(self):
        repo = copy_tree(SAMPLE)
        orphan = repo / ".make-it-lint/checks/orphan_rule.py"
        orphan.write_text(
            "#!/usr/bin/env python3\n"
            "# ---\n"
            "# id: MIL-ORPHAN-001\n"
            "# input: tree\n"
            "# ---\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        os.chmod(orphan, 0o755)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertTrue(
            "neither incident nor origin" in proc.stdout or "does not match any allowed shape" in proc.stdout
        )

    def test_check_id_mismatch(self):
        repo = copy_tree(SAMPLE)
        path = repo / ".make-it-lint/checks/scope_paths.py"
        path.write_text(
            path.read_text(encoding="utf-8").replace("id: MIL-SCOPE-001", "id: MIL-SCOPE-999", 1),
            encoding="utf-8",
        )
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("does not match check.id", proc.stdout)

    def test_expired_waiver(self):
        repo = copy_tree(SAMPLE)
        waivers = repo / ".make-it-lint/waivers.yaml"
        waivers.write_text(
            waivers.read_text(encoding="utf-8").replace("expires: 2027-03-01", "expires: 2026-01-15"),
            encoding="utf-8",
        )
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("expired", proc.stdout)

    def test_waiver_unknown_check_fails_even_when_repo_has_zero_checks(self):
        root = scratch_adopter()
        (root / ".make-it-lint/waivers.yaml").write_text(
            "- check: MIL-NONEXISTENT-001\n"
            "  paths: [config/runner.yaml]\n"
            "  reason: stale waiver pointing to deleted check\n"
            "  approved_by: \"@ledger-owner\"\n"
            "  expires: 2027-03-01\n",
            encoding="utf-8",
        )
        proc = run_mil("lint", "--root", str(root))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("not in .make-it-lint/checks/", proc.stdout)

    def test_wrong_contract(self):
        repo = copy_tree(SAMPLE)
        cfg = repo / ".make-it-lint/config.yaml"
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace(
                "contract: make-it-lint/v1", "contract: mil-harness-contract/v0.0"
            ),
            encoding="utf-8",
        )
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("contract", proc.stdout)

    def test_unknown_config_section_rejected(self):
        root = scratch_adopter()
        cfg = root / ".make-it-lint/config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + "phantom_harness_key: true\n", encoding="utf-8")
        proc = run_mil("lint", "--root", str(root))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("phantom_harness_key", proc.stdout)

    def test_kind_not_in_config(self):
        repo = copy_tree(SAMPLE)
        rec = repo / ".make-it-lint/ledger/F-20260311-01/record.yaml"
        rec.write_text(
            rec.read_text(encoding="utf-8").replace("kind: scope", "kind: mystery"),
            encoding="utf-8",
        )
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("ledger.kinds", proc.stdout)
        self.assertIn("using it anyway", proc.stdout)

    def test_empty_kit_ledger_lints(self):
        root = scratch_adopter()
        proc = run_mil("lint", "--root", str(root))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_history_dates_must_not_go_backwards(self):
        repo = copy_tree(SAMPLE)
        rec = repo / ".make-it-lint/ledger/F-20260311-01/record.yaml"
        text = rec.read_text(encoding="utf-8")
        rec.write_text(text.replace("at: 2026-03-13", "at: 2026-03-01", 1), encoding="utf-8")
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("earlier than the step before it", proc.stdout)

    def test_layer_null_needs_reason(self):
        repo = copy_tree(SAMPLE)
        rec = repo / ".make-it-lint/ledger/F-20260722-01/record.yaml"
        rec.write_text(
            rec.read_text(encoding="utf-8").replace(
                "layer_reason: The default lives in an external launcher; this repository has no hook that can see it.\n",
                "",
            ),
            encoding="utf-8",
        )
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("layer_reason", proc.stdout)

    def test_not_executable_check_and_git_100644_index_mode_are_lint_errors(self):
        repo = copy_tree(SAMPLE)
        (repo / ".make-it-lint/checks/scope_paths.py").chmod(0o644)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("not executable", proc.stdout)

        # Simulate Windows git commit where filesystem has +x locally or WSL,
        # while git index recorded 100644 instead of 100755.
        (repo / ".make-it-lint/checks/scope_paths.py").chmod(0o755)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", ".make-it-lint/checks/scope_paths.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "update-index", "--chmod=-x", ".make-it-lint/checks/scope_paths.py"],
            cwd=repo,
            check=True,
        )
        (repo / ".make-it-lint/checks/scope_paths.py").chmod(0o755)
        proc_git = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc_git.returncode, 1, proc_git.stdout)
        self.assertIn("tracked in git without +x", proc_git.stdout)

    def test_three_waivers_warn_but_do_not_fail(self):
        repo = copy_tree(SAMPLE)
        waivers = repo / ".make-it-lint/waivers.yaml"
        body = waivers.read_text(encoding="utf-8")
        extra = (
            "- check: MIL-SCOPE-001\n"
            "  paths: [config/runner.yaml]\n"
            "  reason: second collect shard migration\n"
            "  approved_by: \"@ledger-owner\"\n"
            "  expires: 2027-04-01\n"
            "- check: MIL-SCOPE-001\n"
            "  paths: [config/runner.yaml]\n"
            "  reason: third collect shard migration\n"
            "  approved_by: \"@ledger-owner\"\n"
            "  expires: 2027-05-01\n"
        )
        waivers.write_text(body.rstrip() + "\n" + extra, encoding="utf-8")
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("3 waivers for MIL-SCOPE-001", proc.stdout)
        self.assertIn("mil lint: ok", proc.stdout)

    def test_diff_and_action_fixtures_validated(self):
        repo = copy_tree(SAMPLE)
        bad_diff = repo / ".make-it-lint/ledger/F-20260311-01/bad"
        (bad_diff / "head").rename(bad_diff / "head.bak")
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("base/ and head/", proc.stdout)
        (bad_diff / "head.bak").rename(bad_diff / "head")

        bad_act = repo / ".make-it-lint/ledger/F-20260905-01/bad"
        (bad_act / "action.json").rename(bad_act / "payload.json")
        proc2 = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc2.returncode, 1, proc2.stdout)
        self.assertIn("action.json", proc2.stdout)

    def test_missing_check_path_is_lint_error(self):
        repo = copy_tree(SAMPLE)
        rec = repo / ".make-it-lint/ledger/F-20260402-01/record.yaml"
        data = load_record_file(rec)
        self.assertEqual(data["enforcement"]["state"], "proven")
        data["check"]["path"] = ".make-it-lint/checks/no_such_check.py"
        save_record_file(rec, data)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("check.path .make-it-lint/checks/no_such_check.py does not exist", proc.stdout)

    def test_null_check_is_reported_not_a_crash(self):
        repo = copy_tree(SAMPLE)
        rec = repo / ".make-it-lint/ledger/F-20260402-01/record.yaml"
        data = load_record_file(rec)
        data["check"] = None
        save_record_file(rec, data)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("NoneType", proc.stdout + proc.stderr)
        self.assertIn("needs a check", proc.stdout)

    def test_agent_approver_and_mismatched_from_state_rejected(self):
        repo = copy_tree(SAMPLE)
        rec = repo / ".make-it-lint/ledger/F-20260311-01/record.yaml"
        data = load_record_file(rec)
        data["history"][-1]["approved_by"] = "cursor-agent"
        data["history"][-1]["from_state"] = "open"
        save_record_file(rec, data)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("promotion is a human-only path", proc.stdout)
        self.assertIn("from_state is 'open' but the previous step is 'proven'", proc.stdout)

    def test_missing_mil_dir_and_empty_check_directory_report_clean_errors(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as bare:
            missing_proc = run_mil("lint", "--root", bare)
            self.assertEqual(missing_proc.returncode, 2)
            self.assertIn("no .make-it-lint directory", missing_proc.stderr)
            self.assertNotIn("NameError", missing_proc.stderr)

        repo = copy_tree(SAMPLE)
        empty_check_dir = Path(repo) / ".make-it-lint/checks/empty_bundle"
        empty_check_dir.mkdir(parents=True)
        rec = Path(repo) / ".make-it-lint/ledger/F-20260402-01/record.yaml"
        data = load_record_file(rec)
        data["check"]["path"] = ".make-it-lint/checks/empty_bundle"
        save_record_file(rec, data)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("check.path .make-it-lint/checks/empty_bundle: directory has no check.* program", proc.stdout)
        self.assertNotIn("NameError", proc.stdout + proc.stderr)
