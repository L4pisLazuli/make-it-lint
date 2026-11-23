from __future__ import annotations

import unittest

from mil import CONTRACT
from tests.support import SAMPLE, copy_tree, run_mil


class CliSmokeTests(unittest.TestCase):
    def test_version(self):
        proc = run_mil("--version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), CONTRACT)

    def test_help_without_command(self):
        proc = run_mil()
        self.assertEqual(proc.returncode, 2)
        text = proc.stdout + proc.stderr
        for cmd in ("record", "classify", "link", "prove", "promote", "demote", "retire", "ci", "guard", "events"):
            self.assertIn(cmd, text)

    def test_status_sample(self):
        proc = run_mil("status", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("shadow", proc.stdout)
        self.assertIn("F-20260311-01", proc.stdout)
        self.assertIn("F-20260722-01", proc.stdout)
        self.assertIn("F-20260905-01", proc.stdout)
        self.assertIn("open", proc.stdout)

    def test_lint_sample(self):
        proc = run_mil("lint", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("mil lint: ok (7 records, 6 checks, 1 waiver, make-it-lint/v1)", proc.stdout)

    def test_root_before_subcommand(self):
        proc = run_mil("--root", str(SAMPLE), "status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("F-20260311-01", proc.stdout)

    def test_lint_self(self):
        proc = run_mil("lint", "--root", ".")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_prove_all_sample(self):
        repo = copy_tree(SAMPLE)
        proc = run_mil("prove", "--all", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("F-20260311-01", proc.stdout)
        self.assertIn("F-20260905-01", proc.stdout)
        self.assertIn("red-on-bad, green-on-good", proc.stdout)
        self.assertNotIn("F-20260722-01", proc.stdout)
        self.assertIn("6 of 6 proved", proc.stdout)

    def test_prove_one(self):
        repo = copy_tree(SAMPLE)
        proc = run_mil("prove", "F-20260518-01", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("F-20260518-01", proc.stdout)
