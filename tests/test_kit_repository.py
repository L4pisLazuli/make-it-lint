from __future__ import annotations

import unittest

from mil.yamlio import load_path
from tests.support import ROOT, TEMPLATE, run_mil


class KitRepositoryTests(unittest.TestCase):
    def test_kit_config_matches_template_ledger(self):
        kit = load_path(str(ROOT / ".make-it-lint" / "config.yaml"))
        tmpl = load_path(str(TEMPLATE / ".make-it-lint" / "config.yaml"))
        self.assertEqual(kit["contract"], tmpl["contract"])
        self.assertEqual(kit["ledger"], tmpl["ledger"])
        self.assertEqual(kit.get("events"), tmpl.get("events"))

    def test_pr_comment_script_is_single_sourced(self):
        self.assertTrue((ROOT / ".github" / "scripts" / "pr-comment.py").is_file())
        self.assertFalse((TEMPLATE / ".github" / "scripts" / "pr-comment.py").exists())

    def test_workflow_uses_in_tree_mil_and_keeps_events(self):
        text = (ROOT / ".github" / "workflows" / "make-it-lint.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("bin/mil lint", text)
        self.assertIn("bin/mil prove --all --dry-run --verify-recorded", text)
        self.assertIn("bin/mil ci", text)
        self.assertIn("--head-ref HEAD", text)
        self.assertIn("retention-days: 90", text)
        self.assertIn("path: mil-events/events.ndjson", text)
        self.assertIn("touch mil-events/events.ndjson", text)
        self.assertIn("python3 .github/scripts/pr-comment.py", text)
        self.assertNotIn("--root examples/sample-repo", text)
        self.assertNotIn("--root examples", text)

    def test_mil_ci_does_not_scan_sample_repo(self):
        proc = run_mil("ci", "--root", str(ROOT), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("examples/sample-repo", combined)
        self.assertIn("0 shadow hit", proc.stdout)
        self.assertIn("0 comment hit", proc.stdout)
        self.assertIn("0 block hit", proc.stdout)
