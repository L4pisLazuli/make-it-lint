from __future__ import annotations

import unittest

from mil.ledger import load_record_file
from tests.support import run_mil, scratch_adopter


class RoutingSuggestionTests(unittest.TestCase):
    def test_record_kind_prints_suggestion_and_does_not_set_layer(self):
        root = scratch_adopter()
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Collect pull request changed config/runner.yaml",
            "--fingerprint",
            "scope:path-outside-allowlist",
            "--source",
            "review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "The PR also changed config/runner.yaml.",
            "--kind",
            "data",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("suggest: kind=data -> layer=verify tool=property", proc.stdout)
        self.assertIn("suggestion only", proc.stdout)
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        self.assertNotEqual(rec.get("layer"), "verify")
        self.assertEqual(rec["enforcement"]["state"], "open")

    def test_config_kind_defaults_override(self):
        root = scratch_adopter()
        cfg = root / ".make-it-lint/config.yaml"
        text = cfg.read_text(encoding="utf-8").replace(
            "layer: verify\n      tool: property", "layer: test\n      tool: test", 1
        )
        cfg.write_text(text, encoding="utf-8")
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Order-total parser drifted from the stored baseline",
            "--fingerprint",
            "data:parser-order-drift",
            "--source",
            "ci",
            "--actor",
            "ci",
            "--reporter",
            "ci",
            "--what",
            "The parser output no longer matched tests/baselines/q3-totals.txt.",
            "--kind",
            "data",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("layer=test tool=test", proc.stdout)

    def test_no_kind_prints_no_suggestion(self):
        root = scratch_adopter()
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Collect pull request changed config/runner.yaml",
            "--fingerprint",
            "scope:path-outside-allowlist",
            "--source",
            "review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "The PR also changed config/runner.yaml.",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("suggest:", proc.stdout)
