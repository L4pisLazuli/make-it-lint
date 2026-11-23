from __future__ import annotations

import unittest
from pathlib import Path

from mil.ledger import load_record_file
from tests.support import ROOT, SAMPLE, copy_tree, run_mil, scratch_adopter

CI_LOG = ROOT / "tests/fixtures/ci/pytest-front-matter.log"
CI_YAML = ROOT / "tests/fixtures/ci/unit-failure.yaml"
GH_LOG = ROOT / "tests/fixtures/ci/gh-run-view.log"


class RecordTests(unittest.TestCase):
    def test_creates_open_record_with_classification(self):
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
            "review comment on a collect pull request",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "Collect is limited to inventory/; the PR also changed config/runner.yaml.",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "ci",
            "--layer-reason",
            "Needs PR kind and the set of changed paths.",
            "--owner",
            "@ledger-owner",
            "--at",
            "2026-03-11",
            "--ref",
            "pull request (anonymized)",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        rec = load_record_file(rec_path)
        self.assertEqual(rec["enforcement"]["state"], "open")
        self.assertNotIn("hits", rec["enforcement"])
        self.assertEqual(rec["classification"]["kind"], "scope")
        self.assertEqual([h["state"] for h in rec["history"]], ["open"])
        self.assertTrue((rec_path.parent / "bad").is_dir())
        self.assertTrue((rec_path.parent / "good").is_dir())

    def test_same_fingerprint_appends_note_and_suggests_layer(self):
        repo = copy_tree(SAMPLE)
        proc = run_mil(
            "record",
            "--root",
            str(repo),
            "--fingerprint",
            "scope:path-outside-allowlist",
            "--at",
            "2026-08-04",
            "--ref",
            "second collect pull request (anonymized)",
            "--note",
            "runner timeout bumped during warehouse import",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(repo / ".make-it-lint/ledger/F-20260311-01/record.yaml")
        self.assertEqual(len(rec["occurrences"]), 2)
        self.assertEqual(rec["occurrences"][1]["at"], "2026-08-04")
        self.assertEqual(rec["occurrences"][1]["note"], "runner timeout bumped during warehouse import")
        self.assertIn("twice", proc.stdout)
        self.assertIn("layer", proc.stdout)

    def test_variants_and_evidence_copy_and_hash(self):
        root = scratch_adopter()
        bad_real = root / "src_bad_real"
        bad_near = root / "src_bad_near"
        good_keep = root / "src_good_keep"
        for d, line in (
            (bad_real, "return lines[:-1]\n"),
            (bad_near, "return lines[1:]\n"),
            (good_keep, "return list(lines)\n"),
        ):
            d.mkdir()
            (d / "pack_pick.py").write_text(f"def pack(lines):\n    {line}", encoding="utf-8")
        ev = root / "counterexample.log"
        ev.write_text("order=ORD-4091 missing SKU BOLT-12 qty=4\n", encoding="utf-8")

        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Packer dropped pick line",
            "--fingerprint",
            "data:pack-drops-pick-line",
            "--source",
            "warehouse pick run",
            "--actor",
            "local-agent",
            "--reporter",
            "developer",
            "--what",
            "pack() dropped a pick-list row.",
            "--kind",
            "data",
            "--decidable",
            "yes",
            "--layer",
            "verify",
            "--layer-reason",
            "Membership invariant holds across generated pick lists.",
            "--bad-variant",
            f"real={bad_real}",
            "--bad-variant",
            f"near-first={bad_near}",
            "--good-variant",
            f"keep-all={good_keep}",
            "--evidence",
            str(ev),
            "--evidence-note",
            "three-SKU warehouse pick trace",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        rec = load_record_file(rec_path)
        self.assertEqual(len(rec["evidence"]), 1)
        self.assertEqual(rec["evidence"][0]["note"], "three-SKU warehouse pick trace")
        self.assertTrue((rec_path.parent / "bad" / "real" / "pack_pick.py").is_file())
        self.assertTrue((rec_path.parent / "bad" / "near-first" / "pack_pick.py").is_file())
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_from_ci_log_and_yaml_and_gh_log(self):
        root = scratch_adopter()
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--from-ci",
            str(CI_LOG),
            "--kind",
            "data",
            "--decidable",
            "yes",
            "--layer",
            "test",
            "--layer-reason",
            "A unit test can pin the parser output.",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        self.assertIn("front_matter", rec["fingerprint"] + rec["title"] + rec["what_happened"])
        self.assertEqual(rec["origin"]["actor"], "ci")

        root2 = scratch_adopter()
        missing_layer = run_mil("record", "--root", str(root2), "--from-ci", str(CI_YAML))
        self.assertEqual(missing_layer.returncode, 2)
        self.assertIn("--layer-reason", missing_layer.stderr)
        ok_yaml = run_mil(
            "record",
            "--root",
            str(root2),
            "--from-ci",
            str(CI_YAML),
            "--layer",
            "test",
            "--layer-reason",
            "A unit test can pin the parser output.",
        )
        self.assertEqual(ok_yaml.returncode, 0, ok_yaml.stdout + ok_yaml.stderr)

        root3 = scratch_adopter()
        gh_proc = run_mil("record", "--root", str(root3), "--from-ci", str(GH_LOG))
        self.assertEqual(gh_proc.returncode, 0, gh_proc.stdout + gh_proc.stderr)
        gh_rec = load_record_file(next((root3 / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        self.assertTrue(str(gh_rec["fingerprint"]).startswith("ci:"))
        self.assertEqual(gh_rec["enforcement"]["state"], "open")

    def test_from_ci_run_id_is_rejected(self):
        proc = run_mil("record", "--from-ci", "1842567")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does not fetch GitHub run ids", proc.stderr)

    def test_decidable_no_goes_to_human_review(self):
        root = scratch_adopter()
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
            "--owner",
            "@ledger-owner",
            "--at",
            "2026-09-03",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        self.assertEqual(rec["enforcement"]["state"], "human_review")
        self.assertEqual([h["state"] for h in rec["history"]], ["human_review"])

    def test_bad_and_good_copy_into_fixture_dirs(self):
        root = scratch_adopter()
        tree = Path(root) / "failing"
        (tree / "src").mkdir(parents=True)
        (tree / "src" / "app.py").write_text("breakpoint()\n", encoding="utf-8")
        fixed = Path(root) / "fixed_app.py"
        fixed.write_text("def run_worker():\n    return 0\n", encoding="utf-8")
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--fingerprint",
            "lint:breakpoint-left",
            "--title",
            "Left breakpoint() in worker module",
            "--what",
            "breakpoint() was committed in src/app.py.",
            "--source",
            "review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--bad",
            str(tree),
            "--good",
            str(fixed),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec_dir = next((root / ".make-it-lint/ledger").glob("F-*"))
        self.assertTrue((rec_dir / "bad" / "src" / "app.py").is_file())
        self.assertTrue((rec_dir / "good" / "fixed_app.py").is_file())
        self.assertIn("bad/ <- contents of %s: src/" % tree, proc.stdout)

    def test_fingerprint_must_look_like_kind_slug(self):
        root = scratch_adopter()
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--fingerprint",
            "Bad Print",
            "--title",
            "Invalid fingerprint format",
            "--what",
            "Spaces and uppercase are invalid in fingerprints.",
            "--source",
            "review comment",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must look like", proc.stderr)
        self.assertEqual(list((root / ".make-it-lint/ledger").glob("F-*")), [])

    def test_second_occurrence_suggests_stronger_layer(self):
        from mil.cmd_record import raise_layer_advice

        self.assertIn("hook", raise_layer_advice({"layer": "ci", "check": {"id": "MIL-SCOPE-001"}, "enforcement": {"state": "shadow"}}))
        lint_advice = raise_layer_advice({"layer": "lint", "check": {"id": "MIL-BP-001"}, "enforcement": {"state": "block"}})
        self.assertIn("type", lint_advice)
        self.assertIn("raise one level", lint_advice)
        self.assertIn("architecture", lint_advice)
        self.assertIn("human_review", raise_layer_advice({"enforcement": {"state": "human_review"}}))
        self.assertIn("no layer", raise_layer_advice({"layer": None, "enforcement": {"state": "open"}}))

    def test_classification_without_layer_reason_is_rejected_and_doc_layer_removed(self):
        root = scratch_adopter()
        for extra in (
            ("--kind", "scope", "--decidable", "yes"),
            ("--kind", "scope", "--decidable", "partial"),
            ("--kind", "scope", "--decidable", "yes", "--layer", "ci"),
        ):
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
                *extra,
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("--layer-reason", proc.stderr)

        doc_proc = run_mil("record", "--root", str(root), "--layer", "doc")
        self.assertEqual(doc_proc.returncode, 2)
        self.assertIn("invalid choice", doc_proc.stderr)

    def test_no_layer_records_null_layer_and_lints(self):
        root = scratch_adopter()
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "External launcher selected fast mode",
            "--fingerprint",
            "boot:disallowed-fast-default",
            "--source",
            "session review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "developer",
            "--what",
            "The hosted launcher picked a disallowed default.",
            "--kind",
            "boot",
            "--decidable",
            "partial",
            "--no-layer",
            "--layer-reason",
            "Setting lives in an external launcher outside the repository.",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("suggest: kind", proc.stdout)
        rec = load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))
        self.assertEqual(rec["enforcement"]["state"], "open")
        self.assertIsNone(rec["layer"])
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_explicit_layer_suppresses_contradicting_kind_suggestion_and_repeat_warns(self):
        root = scratch_adopter()
        explicit = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Left breakpoint() in inventory parser",
            "--fingerprint",
            "scope:breakpoint-in-parser",
            "--source",
            "review comment",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "A debug breakpoint() remained in src/parser.py.",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "lint",
            "--layer-reason",
            "Visible in a single Python file without git diff.",
        )
        self.assertEqual(explicit.returncode, 0, explicit.stdout + explicit.stderr)
        self.assertNotIn("suggest: kind", explicit.stdout)

        repeat = run_mil(
            "record",
            "--root",
            str(root),
            "--fingerprint",
            "scope:breakpoint-in-parser",
            "--layer",
            "ci",
        )
        self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
        self.assertIn("note: --layer ci is not applied to an existing record", repeat.stdout)

        unclassified_with_kind = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Collect PR touched runner config",
            "--fingerprint",
            "scope:runner-config-edit",
            "--source",
            "review comment",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "Collect PR modified config/runner.yaml.",
            "--kind",
            "scope",
            "--decidable",
            "no",
        )
        self.assertEqual(unclassified_with_kind.returncode, 0, unclassified_with_kind.stdout + unclassified_with_kind.stderr)
        self.assertIn("suggest: kind=scope -> layer=ci", unclassified_with_kind.stdout)
