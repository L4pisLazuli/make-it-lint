from __future__ import annotations

import os
import unittest

from mil.ledger import load_record_file
from tests.support import SAMPLE, copy_tree, run_mil, scratch_adopter

CHECK_BODY = '''#!/usr/bin/env python3
# ---
# id: MIL-SETTINGS-001
# incident: {incident}
# input: tree
# layer: lint
# tool: script
# ---
"""Fail when config/settings.yaml is present in a collect tree."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--root")
parser.add_argument("--config")
args = parser.parse_args()
root = Path(args.root)
hits = sorted(p for p in root.rglob("settings.yaml") if p.is_file())
if hits:
    for path in hits:
        print("%s: collect trees must not include config/settings.yaml" % path.relative_to(root).as_posix())
    raise SystemExit(1)
raise SystemExit(0)
'''


class LinkTests(unittest.TestCase):
    def test_link_then_prove_reaches_proven(self):
        root = scratch_adopter()
        bad = root / "failing-collect"
        good = root / "fixed-collect"
        (bad / "config").mkdir(parents=True)
        (bad / "config" / "settings.yaml").write_text("timeout_seconds: 30\n", encoding="utf-8")
        (good / "inventory").mkdir(parents=True)
        (good / "inventory" / "items.yaml").write_text("sku: WIDGET-04\nqty: 12\n", encoding="utf-8")
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Collect PR shipped config/settings.yaml",
            "--fingerprint",
            "scope:settings-in-collect",
            "--source",
            "review comment on a collect pull request",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "Collect is limited to inventory/; the PR also added config/settings.yaml.",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "lint",
            "--layer-reason",
            "The forbidden file is visible in the tree without PR metadata.",
            "--bad",
            str(bad),
            "--good",
            str(good),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        incident = rec["id"]
        check = root / ".make-it-lint/checks/settings_in_collect.py"
        check.write_text(CHECK_BODY.format(incident=incident), encoding="utf-8")
        os.chmod(check, 0o755)
        proc = run_mil("link", incident, ".make-it-lint/checks/settings_in_collect.py", "--root", str(root))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(root / ".make-it-lint/ledger" / incident / "record.yaml")
        self.assertEqual(rec["enforcement"]["state"], "open")
        self.assertEqual(rec["check"]["id"], "MIL-SETTINGS-001")
        self.assertEqual(rec["check"]["tool"], "script")
        self.assertEqual(rec["check"]["path"], ".make-it-lint/checks/settings_in_collect.py")
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        prove = run_mil("prove", incident, "--root", str(root))
        self.assertEqual(prove.returncode, 0, prove.stdout + prove.stderr)
        rec = load_record_file(root / ".make-it-lint/ledger" / incident / "record.yaml")
        self.assertEqual(rec["enforcement"]["state"], "proven")

    def test_link_is_idempotent_when_already_attached(self):
        repo = copy_tree(SAMPLE)
        before_msg = load_record_file(repo / ".make-it-lint/ledger/F-20260311-01/record.yaml")["check"]["message"]
        proc = run_mil(
            "link",
            "F-20260311-01",
            ".make-it-lint/checks/scope_paths.py",
            "--message",
            "New wording that should not overwrite an attached check",
            "--root",
            str(repo),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(repo / ".make-it-lint/ledger/F-20260311-01/record.yaml")
        self.assertEqual(rec["check"]["id"], "MIL-SCOPE-001")
        self.assertEqual(rec["check"]["message"], before_msg)
        self.assertEqual(rec["enforcement"]["state"], "shadow")
        self.assertIn("already attached; record.check kept as stored", proc.stdout)
        self.assertIn("note: record.check.message is", proc.stdout)
        self.assertIn("not rewritten", proc.stdout)

    def test_link_rejects_unclassified_record(self):
        root = scratch_adopter()
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Collect PR changed config/runner.yaml",
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
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        check = root / ".make-it-lint/checks/settings_in_collect.py"
        check.write_text(CHECK_BODY.format(incident=rec["id"]), encoding="utf-8")
        os.chmod(check, 0o755)
        proc = run_mil("link", rec["id"], str(check), "--root", str(root))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("mil classify", proc.stderr)

    def test_check_without_record_makes_lint_red_until_link(self):
        root = scratch_adopter()
        bad = root / "failing-collect"
        good = root / "fixed-collect"
        (bad / "config").mkdir(parents=True)
        (bad / "config" / "settings.yaml").write_text("timeout_seconds: 30\n", encoding="utf-8")
        (good / "inventory").mkdir(parents=True)
        (good / "inventory" / "items.yaml").write_text("sku: WIDGET-04\nqty: 12\n", encoding="utf-8")
        run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Collect PR shipped config/settings.yaml",
            "--fingerprint",
            "scope:settings-in-collect",
            "--source",
            "review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "The PR also added config/settings.yaml.",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "lint",
            "--layer-reason",
            "The forbidden file is visible in the tree without PR metadata.",
            "--bad",
            str(bad),
            "--good",
            str(good),
        )
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        check = root / ".make-it-lint/checks/settings_in_collect.py"
        check.write_text(CHECK_BODY.format(incident=rec["id"]), encoding="utf-8")
        os.chmod(check, 0o755)
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 1, lint.stdout)
        self.assertIn("does not match check.id", lint.stdout)
        link = run_mil("link", rec["id"], ".make-it-lint/checks/settings_in_collect.py", "--root", str(root))
        self.assertEqual(link.returncode, 0, link.stdout + link.stderr)
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_link_auto_sets_executable_bit_and_git_100755_mode(self):
        import subprocess

        root = scratch_adopter()
        bad = root / "failing-collect"
        good = root / "fixed-collect"
        (bad / "config").mkdir(parents=True)
        (bad / "config" / "settings.yaml").write_text("timeout_seconds: 30\n", encoding="utf-8")
        (good / "inventory").mkdir(parents=True)
        (good / "inventory" / "items.yaml").write_text("sku: WIDGET-04\nqty: 12\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)

        run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Collect PR shipped config/settings.yaml",
            "--fingerprint",
            "scope:settings-in-collect",
            "--source",
            "review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "@reviewer",
            "--what",
            "The PR also added config/settings.yaml.",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "lint",
            "--layer-reason",
            "The forbidden file is visible in the tree.",
            "--bad",
            str(bad),
            "--good",
            str(good),
        )
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        check = root / ".make-it-lint/checks/settings_in_collect.py"
        check.write_text(CHECK_BODY.format(incident=rec["id"]), encoding="utf-8")
        check.chmod(0o644)
        subprocess.run(["git", "add", ".make-it-lint/checks/settings_in_collect.py"], cwd=root, check=True)
        subprocess.run(
            ["git", "update-index", "--chmod=-x", ".make-it-lint/checks/settings_in_collect.py"],
            cwd=root,
            check=True,
        )
        check.chmod(0o644)

        link = run_mil("link", rec["id"], ".make-it-lint/checks/settings_in_collect.py", "--root", str(root))
        self.assertEqual(link.returncode, 0, link.stdout + link.stderr)
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
