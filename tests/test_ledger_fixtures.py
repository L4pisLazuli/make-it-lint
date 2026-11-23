from __future__ import annotations

import os
import sys
import unittest

from tests.support import SAMPLE, copy_tree, run_mil, scratch_adopter

from mil.paths import is_ledger_fixture_path


SECRET_CHECK = '''#!/usr/bin/env python3
# ---
# id: MIL-SECRET-001
# origin: test-secret
# input: tree
# enforcement:
#   state: block
# ---
"""Fail when a product file contains the leaked collect token."""
import argparse
from pathlib import Path

from mil.paths import is_ledger_fixture_path

parser = argparse.ArgumentParser()
parser.add_argument("--root")
parser.add_argument("--config")
args = parser.parse_args()
root = Path(args.root)
needle = "leaked-" + "collect-token"
hits = 0
for path in root.rglob("*"):
    if not path.is_file():
        continue
    rel = path.relative_to(root).as_posix()
    if is_ledger_fixture_path(rel):
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        continue
    if needle in text:
        print("%s: secret value in the product tree" % rel)
        hits += 1
raise SystemExit(1 if hits else 0)
'''


EMIT_LEDGER_HIT = '''#!/usr/bin/env python3
# ---
# id: MIL-SECRET-002
# origin: test-secret-emit
# input: tree
# enforcement:
#   state: block
# ---
"""Always emit a ledger fixture path so mil ci must drop it."""
print(".make-it-lint/ledger/F-20260115-01/bad/app.env: secret value in the product tree")
raise SystemExit(1)
'''


class LedgerFixtureSkipTests(unittest.TestCase):
    def test_is_ledger_fixture_path(self):
        self.assertTrue(
            is_ledger_fixture_path(
                ".make-it-lint/ledger/F-20260311-01/bad/head/config/runner.yaml"
            )
        )
        self.assertTrue(
            is_ledger_fixture_path(".make-it-lint/ledger/F-20260518-01/good/run_suite.py")
        )
        self.assertFalse(is_ledger_fixture_path("config/runner.yaml"))
        self.assertFalse(is_ledger_fixture_path(".make-it-lint/checks/scope_paths.py"))

    def test_empty_product_tree_with_ledger_bad_does_not_block(self):
        root = scratch_adopter()
        check = root / ".make-it-lint/checks/secret_scan.py"
        check.write_text(SECRET_CHECK, encoding="utf-8")
        os.chmod(check, 0o755)
        rec = root / ".make-it-lint/ledger/F-20260115-01"
        (rec / "bad").mkdir(parents=True)
        (rec / "good").mkdir()
        (rec / "bad" / "app.env").write_text(
            "COLLECT_TIMEOUT=30\nSECRET=leaked-collect-token\n", encoding="utf-8"
        )
        (rec / "good" / "app.env").write_text("COLLECT_TIMEOUT=30\n", encoding="utf-8")
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("0 block hit", proc.stdout)
        self.assertNotIn("app.env: secret value", proc.stdout)

    def test_product_secret_still_blocks(self):
        root = scratch_adopter()
        check = root / ".make-it-lint/checks/secret_scan.py"
        check.write_text(SECRET_CHECK, encoding="utf-8")
        os.chmod(check, 0o755)
        (root / "app.env").write_text("SECRET=leaked-collect-token\n", encoding="utf-8")
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("1 block hit", proc.stdout)
        self.assertIn("app.env", proc.stdout)

    def test_ci_drops_ledger_paths_even_when_check_emits_them(self):
        root = scratch_adopter()
        check = root / ".make-it-lint/checks/secret_emit.py"
        check.write_text(EMIT_LEDGER_HIT, encoding="utf-8")
        os.chmod(check, 0o755)
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("0 block hit", proc.stdout)
        self.assertNotIn("app.env: secret value", proc.stdout)

    def test_rel_files_skips_ledger_fixtures(self):
        repo = copy_tree(SAMPLE)
        sys.path.insert(0, str(repo / ".make-it-lint/checks"))
        from _support import rel_files

        (repo / "inventory").mkdir(exist_ok=True)
        (repo / "inventory" / "items.yaml").write_text("sku: WIDGET-04\nqty: 12\n", encoding="utf-8")
        files = rel_files(repo)
        self.assertIn("inventory/items.yaml", files)
        leaked = [name for name in files if is_ledger_fixture_path(name)]
        self.assertEqual(leaked, [])
