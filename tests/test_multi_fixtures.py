from __future__ import annotations

import unittest

from mil.ledger import load_record_file, save_record_file
from tests.support import SAMPLE, copy_tree, run_mil
from tests.test_prove import weaken


class MultiFixtureTests(unittest.TestCase):
    def test_sample_pack_record_proves_multi_form(self):
        proc = run_mil("prove", "F-20260814-01", "--dry-run", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("red-on-bad 2/2, green-on-good 2/2", proc.stdout)

    def test_non_near_named_bad_variant_is_also_executed(self):
        repo = copy_tree(SAMPLE)
        extra_bad = repo / ".make-it-lint/ledger/F-20260814-01/bad/duplicate-sku-drop"
        extra_bad.mkdir()
        (extra_bad / "pack_pick.py").write_text(
            '"""Drops duplicate SKU rows while packing."""\n\n'
            "def pack(lines):\n"
            "    seen = set()\n"
            "    out = []\n"
            "    for row in lines:\n"
            "        if row['sku'] not in seen:\n"
            "            seen.add(row['sku'])\n"
            "            out.append(row)\n"
            "    return out\n",
            encoding="utf-8",
        )
        proc = run_mil("prove", "F-20260814-01", "--dry-run", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("red-on-bad 3/3, green-on-good 2/2", proc.stdout)

    def test_stray_file_in_multi_fixture_fails_lint_and_prove(self):
        repo = copy_tree(SAMPLE)
        stray = repo / ".make-it-lint/ledger/F-20260814-01/bad/stray_pick.py"
        stray.write_text("def pack(lines):\n    return lines[:-1]\n", encoding="utf-8")
        lint = run_mil("lint", "--root", str(repo))
        self.assertEqual(lint.returncode, 1, lint.stdout)
        self.assertIn("stray file in multi-variant bad/", lint.stdout)
        prove = run_mil("prove", "F-20260814-01", "--dry-run", "--root", str(repo))
        self.assertEqual(prove.returncode, 1, prove.stdout)

    def test_single_pair_form_still_proves(self):
        proc = run_mil("prove", "F-20260518-01", "--dry-run", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("red-on-bad, green-on-good", proc.stdout)
        self.assertNotIn("red-on-bad 1/1", proc.stdout)

    def test_one_near_miss_staying_green_fails(self):
        repo = copy_tree(SAMPLE)
        near = repo / ".make-it-lint/ledger/F-20260814-01/bad/near-drop-first/pack_pick.py"
        near.write_text(
            '"""Pack that already keeps every line; a near-miss that the check must still catch."""\n\n'
            "def pack(lines):\n"
            "    return list(lines)\n",
            encoding="utf-8",
        )
        proc = run_mil("prove", "F-20260814-01", "--dry-run", "--root", str(repo))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("red-on-bad 1/2", proc.stdout)
        self.assertIn("bad/near-drop-first: exit 0, the check did not catch the failure", proc.stdout)

    def test_writes_multi_last_result(self):
        repo = copy_tree(SAMPLE)
        rec_path = repo / ".make-it-lint/ledger/F-20260814-01/record.yaml"
        rec = load_record_file(rec_path)
        rec["proof"]["last_result"] = None
        save_record_file(rec_path, rec)
        proc = run_mil("prove", "F-20260814-01", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(rec_path)
        self.assertEqual(rec["proof"]["last_result"], "red-on-bad 2/2, green-on-good 2/2")

    def test_weakened_pack_check_fails_multi_proof(self):
        repo = copy_tree(SAMPLE)
        weaken(repo / ".make-it-lint/checks/pack_invariant.py", "import sys\nsys.exit(0)\n")
        proc = run_mil("prove", "F-20260814-01", "--root", str(repo))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("red-on-bad 0/2", proc.stdout)
