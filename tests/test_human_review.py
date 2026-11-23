from __future__ import annotations

import unittest

from tests.support import run_mil, scratch_adopter


class HumanReviewDisplayTests(unittest.TestCase):
    def _record_human_review(self, root):
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Started implementation before the research notes were written",
            "--fingerprint",
            "judgment:work-before-research",
            "--source",
            "reviewer note",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "Implementation commits landed before the measurement plan existed.",
            "--kind",
            "judgment",
            "--decidable",
            "no",
            "--at",
            "2026-09-03",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = next((root / ".make-it-lint/ledger").glob("F-*"))
        return rec.name

    def test_prove_all_prints_awaiting_human_review(self):
        root = scratch_adopter()
        rid = self._record_human_review(root)
        proc = run_mil("prove", "--all", "--dry-run", "--root", str(root))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(rid, proc.stdout)
        self.assertIn("awaiting human review", proc.stdout)
        self.assertNotRegex(proc.stdout, r"^skip\s+" + rid, proc.stdout)

    def test_prove_id_prints_awaiting_human_review(self):
        root = scratch_adopter()
        rid = self._record_human_review(root)
        proc = run_mil("prove", rid, "--root", str(root))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("awaiting human review", proc.stdout)
        self.assertNotIn("nothing to prove", proc.stdout)

    def test_ci_prints_awaiting_human_review(self):
        root = scratch_adopter()
        rid = self._record_human_review(root)
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("review %s: awaiting human review" % rid, proc.stdout.splitlines())
        self.assertNotIn("review %s %s:" % (rid, rid), proc.stdout)
        self.assertIn("1 awaiting human review", proc.stdout)
        skip_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("skip") and rid in ln]
        self.assertEqual(skip_lines, [])

    def test_classified_without_check_is_still_nothing_to_prove(self):
        from tests.support import SAMPLE

        proc = run_mil("prove", "F-20260722-01", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("nothing to prove", proc.stdout)
        self.assertNotIn("awaiting human review", proc.stdout)
