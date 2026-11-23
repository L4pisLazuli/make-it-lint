from __future__ import annotations

import unittest

from tests.support import SAMPLE, copy_tree, run_mil, scratch_adopter

from mil.ledger import load_record_file


class UnregisteredKindTests(unittest.TestCase):
    def test_dependency_kind_lints_with_warning(self):
        repo = copy_tree(SAMPLE)
        rec = repo / ".make-it-lint/ledger/F-20260311-01/record.yaml"
        rec.write_text(
            rec.read_text(encoding="utf-8").replace("kind: scope", "kind: dependency"),
            encoding="utf-8",
        )
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("classification.kind 'dependency' is not in config ledger.kinds", proc.stdout)
        self.assertIn("using it anyway", proc.stdout)
        self.assertIn("mil lint: ok", proc.stdout)

    def test_record_accepts_kind_not_in_config(self):
        root = scratch_adopter()
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Pinned wheel hash drifted from the lock file",
            "--fingerprint",
            "dependency:lockfile-hash-drift",
            "--source",
            "ci log",
            "--actor",
            "cloud-agent",
            "--reporter",
            "ci",
            "--what",
            "The lock file hash no longer matched the pinned wheel.",
            "--kind",
            "dependency",
            "--decidable",
            "yes",
            "--layer",
            "ci",
            "--layer-reason",
            "Needs the lock file and the resolved wheel set from the PR.",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        self.assertEqual(rec["classification"]["kind"], "dependency")
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertIn("using it anyway", lint.stdout)
