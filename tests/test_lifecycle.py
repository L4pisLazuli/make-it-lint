"""End-to-end tests for mil classify, mil demote, and mil retire."""

from __future__ import annotations

import unittest

from mil.ledger import load_record_file
from tests.support import ROOT, SAMPLE, copy_tree, run_mil, run_mil_tty, scratch_adopter

GH_LOG = ROOT / "tests/fixtures/ci/gh-run-view.log"


class LifecycleCommandTests(unittest.TestCase):
    def test_from_ci_then_classify_completes_open_record(self):
        root = scratch_adopter()
        rec_proc = run_mil("record", "--root", str(root), "--from-ci", str(GH_LOG))
        self.assertEqual(rec_proc.returncode, 0, rec_proc.stdout + rec_proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        rid = rec_path.parent.name
        before = load_record_file(rec_path)
        self.assertNotIn("classification", before)

        cls_proc = run_mil(
            "classify",
            rid,
            "--root",
            str(root),
            "--kind",
            "data",
            "--decidable",
            "yes",
            "--layer",
            "test",
            "--layer-reason",
            "Unit test on score.py pins rounding behavior.",
        )
        self.assertEqual(cls_proc.returncode, 0, cls_proc.stdout + cls_proc.stderr)
        self.assertNotIn("suggest: kind=", cls_proc.stdout)
        after = load_record_file(rec_path)
        self.assertEqual(after["classification"], {"kind": "data", "decidable": "yes"})
        self.assertEqual(after["layer"], "test")
        self.assertEqual(after["enforcement"]["state"], "open")
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_classify_roundtrips_between_open_and_human_review(self):
        root = scratch_adopter()
        run_mil("record", "--root", str(root), "--from-ci", str(GH_LOG))
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        rid = rec_path.parent.name

        to_hr = run_mil(
            "classify",
            rid,
            "--root",
            str(root),
            "--kind",
            "judgment",
            "--decidable",
            "no",
        )
        self.assertEqual(to_hr.returncode, 0, to_hr.stdout + to_hr.stderr)
        self.assertEqual(load_record_file(rec_path)["enforcement"]["state"], "human_review")

        back_open = run_mil(
            "classify",
            rid,
            "--root",
            str(root),
            "--kind",
            "data",
            "--decidable",
            "yes",
            "--layer",
            "verify",
            "--layer-reason",
            "Found a deterministic property invariant for score calculation.",
        )
        self.assertEqual(back_open.returncode, 0, back_open.stdout + back_open.stderr)
        rec = load_record_file(rec_path)
        self.assertEqual(rec["enforcement"]["state"], "open")
        self.assertEqual([h["state"] for h in rec["history"]], ["open", "human_review", "open"])
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_classify_refuses_proven_record(self):
        repo = copy_tree(SAMPLE)
        proc = run_mil(
            "classify",
            "F-20260402-01",
            "--root",
            str(repo),
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "ci",
            "--layer-reason",
            "Already proven.",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot reclassify", proc.stderr)

    def test_demote_steps_down_one_level_at_a_time(self):
        repo = copy_tree(SAMPLE)
        # Promote F-20260609-01 (currently comment) -> block first
        self.assertEqual(
            run_mil_tty(
                "block\n",
                "promote",
                "F-20260609-01",
                "--approved-by",
                "@ledger-owner",
                "--root",
                str(repo),
            ).returncode,
            0,
        )
        # Agent name in --by is refused so record.yaml stays lint-clean
        agent_demote = run_mil(
            "demote",
            "F-20260609-01",
            "--reason",
            "noisy on emergency hotfix",
            "--by",
            "cloud-agent",
            "--root",
            str(repo),
        )
        self.assertEqual(agent_demote.returncode, 2)
        self.assertIn("names an agent", agent_demote.stderr)

        # Skipping block -> shadow is refused
        skip = run_mil(
            "demote",
            "F-20260609-01",
            "--to",
            "shadow",
            "--reason",
            "noisy on emergency hotfix",
            "--root",
            str(repo),
        )
        self.assertEqual(skip.returncode, 2)
        self.assertIn("skips a state", skip.stderr)

        # Step down block -> comment -> shadow -> proven
        for expected in ("comment", "shadow", "proven"):
            step = run_mil(
                "demote",
                "F-20260609-01",
                "--reason",
                f"stepping down to {expected} while refining catalog paths",
                "--by",
                "@ledger-owner",
                "--root",
                str(repo),
            )
            self.assertEqual(step.returncode, 0, step.stdout + step.stderr)
            rec = load_record_file(repo / ".make-it-lint/ledger/F-20260609-01/record.yaml")
            self.assertEqual(rec["enforcement"]["state"], expected)

        # Cannot demote below proven
        too_low = run_mil("demote", "F-20260609-01", "--reason", "below proven", "--root", str(repo))
        self.assertEqual(too_low.returncode, 2)
        lint = run_mil("lint", "--root", str(repo))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_retire_stops_product_execution_and_keeps_ledger_valid(self):
        repo = copy_tree(SAMPLE)
        agent_try = run_mil(
            "retire",
            "F-20260609-01",
            "--reason",
            "derived catalog removed from git",
            "--by",
            "cloud-agent",
            "--root",
            str(repo),
        )
        self.assertEqual(agent_try.returncode, 2)
        self.assertIn("human decision", agent_try.stderr)

        ok = run_mil(
            "retire",
            "F-20260609-01",
            "--reason",
            "generated/catalog.json moved to build artifacts and removed from git",
            "--by",
            "@ledger-owner",
            "--root",
            str(repo),
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        rec = load_record_file(repo / ".make-it-lint/ledger/F-20260609-01/record.yaml")
        self.assertEqual(rec["enforcement"]["state"], "retired")
        lint = run_mil("lint", "--root", str(repo))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

        ci = run_mil("ci", "--root", str(repo), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(ci.returncode, 0, ci.stdout + ci.stderr)
        self.assertIn("skip  MIL-GEN-001 F-20260609-01: state retired is not run against product code", ci.stdout)
