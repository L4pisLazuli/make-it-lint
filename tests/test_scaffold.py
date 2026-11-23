"""Tests for `mil scaffold` check generation, defaults, and safety invariants."""

from __future__ import annotations

import unittest

from mil.checks import extract_metadata
from mil.ledger import load_record_file
from tests.support import SAMPLE, copy_tree, run_mil, scratch_adopter


class ScaffoldTests(unittest.TestCase):
    def test_scaffold_tree_diff_and_action_defaults_and_prove_lifecycle(self):
        root = scratch_adopter()
        bad_dir = root / "tmp_fx" / "bad"
        good_dir = root / "tmp_fx" / "good"
        (bad_dir / "src").mkdir(parents=True)
        (good_dir / "src").mkdir(parents=True)
        (bad_dir / "src" / "handler.py").write_text(
            "def handle_order(order):\n    breakpoint()\n    return order\n",
            encoding="utf-8",
        )
        (good_dir / "src" / "handler.py").write_text(
            "def handle_order(order):\n    return order\n",
            encoding="utf-8",
        )

        rec_proc = run_mil(
            "record",
            "--root",
            str(root),
            "--fingerprint",
            "lint:leftover-breakpoint",
            "--title",
            "Left breakpoint() in order handler",
            "--what",
            "Agent committed a debug breakpoint() call in src/handler.py.",
            "--source",
            "PR review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "@reviewer",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "lint",
            "--layer-reason",
            "Visible in a single Python source file without diff context.",
            "--bad",
            str(bad_dir),
            "--good",
            str(good_dir),
        )
        self.assertEqual(rec_proc.returncode, 0, rec_proc.stdout + rec_proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        incident_id = load_record_file(rec_path)["id"]

        scaf = run_mil("scaffold", incident_id, "--no-draft", "--root", str(root))
        self.assertEqual(scaf.returncode, 0, scaf.stdout + scaf.stderr)
        self.assertIn("MIL-LINT-001", scaf.stdout)
        check_path = root / ".make-it-lint/checks/leftover_breakpoint.py"
        self.assertTrue(check_path.is_file())
        meta = extract_metadata(check_path)
        self.assertEqual(meta["id"], "MIL-LINT-001")
        self.assertEqual(meta["incident"], incident_id)
        self.assertEqual(meta["input"], "tree")
        self.assertEqual(meta["layer"], "lint")
        self.assertEqual(meta["tool"], "script")

        # Ledger lint is immediately green after scaffold
        lint_before = run_mil("lint", "--root", str(root))
        self.assertEqual(lint_before.returncode, 0, lint_before.stderr + lint_before.stdout)

        # Unedited `--no-draft` scaffold exits 2 inside the check, so `mil prove` fails (exit 1)
        unedited_prove = run_mil("prove", incident_id, "--root", str(root))
        self.assertEqual(unedited_prove.returncode, 1, unedited_prove.stdout + unedited_prove.stderr)
        self.assertIn("exit 2 (2 means the check is broken)", unedited_prove.stdout)
        self.assertIn("unimplemented check scaffold", unedited_prove.stdout + unedited_prove.stderr)
        self.assertEqual(load_record_file(rec_path)["enforcement"]["state"], "open")

        # Implement the tree rule inside the generated scaffold and prove
        text = check_path.read_text(encoding="utf-8")
        old_placeholder = (
            "        # Replace the two lines below with your tree check appending '<rel_path>[:<line>]: <how to fix>' to `hits`.\n"
            "        break\n"
            '    sys.stderr.write(".make-it-lint/checks/leftover_breakpoint.py: unimplemented check scaffold; edit the rule logic before running mil prove\\n")\n'
            "    return 2"
        )
        new_logic = (
            '        if path.suffix == ".py":\n'
            '            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):\n'
            '                if line.strip().startswith("breakpoint()"):\n'
            '                    hits.append(f"{path.relative_to(root).as_posix()}:{lineno}: remove breakpoint() before committing")'
        )
        self.assertIn(old_placeholder, text)
        check_path.write_text(text.replace(old_placeholder, new_logic, 1), encoding="utf-8")

        proved = run_mil("prove", incident_id, "--root", str(root))
        self.assertEqual(proved.returncode, 0, proved.stdout + proved.stderr)
        self.assertEqual(load_record_file(rec_path)["enforcement"]["state"], "proven")

        # Also verify `layer=ci -> input=diff` and `layer=hook -> input=action` defaults + ID increment
        for fp, layer_name, expected_input, expected_tool, expected_id in (
            ("lint:second-lint-issue", "lint", "tree", "script", "MIL-LINT-002"),
            ("scope:forbidden-config-touch", "ci", "diff", "script", "MIL-SCOPE-001"),
            ("hook:destructive-reset", "hook", "action", "hook", "MIL-HOOK-001"),
        ):
            r = run_mil(
                "record",
                "--root",
                str(root),
                "--fingerprint",
                fp,
                "--title",
                f"Check default mapping for {fp}",
                "--what",
                f"Recorded {fp} to verify scaffold defaults.",
                "--source",
                "PR review",
                "--actor",
                "cloud-agent",
                "--reporter",
                "@reviewer",
                "--kind",
                "scope",
                "--decidable",
                "yes",
                "--layer",
                layer_name,
                "--layer-reason",
                f"Stop at {layer_name} layer.",
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            rid = sorted(p.parent.name for p in (root / ".make-it-lint/ledger").glob("F-*/record.yaml"))[-1]
            s = run_mil("scaffold", rid, "--root", str(root))
            self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
            rec_now = load_record_file(root / ".make-it-lint/ledger" / rid / "record.yaml")
            self.assertEqual(rec_now["check"]["id"], expected_id)
            self.assertEqual(rec_now["check"]["tool"], expected_tool)
            m_now = extract_metadata(root / rec_now["check"]["path"])
            self.assertEqual(m_now["input"], expected_input)

    def test_scaffold_refuses_existing_check_and_invalid_states(self):
        repo = copy_tree(SAMPLE)
        # Non-open record (`proven`) is refused
        non_open = run_mil("scaffold", "F-20260402-01", "--root", str(repo))
        self.assertEqual(non_open.returncode, 2)
        self.assertIn("only creates a check for open records", non_open.stderr)

        # Unclassified record (`layer: null` on F-20260722-01) is refused
        no_layer = run_mil("scaffold", "F-20260722-01", "--root", str(repo))
        self.assertEqual(no_layer.returncode, 2)
        self.assertIn("classify the record with a stop layer first", no_layer.stderr)

        # Overwriting an existing check file is refused
        rec_proc = run_mil(
            "record",
            "--root",
            str(repo),
            "--fingerprint",
            "scope:duplicate-target-path",
            "--title",
            "Attempted scaffold onto existing check path",
            "--what",
            "Tried to overwrite scope_paths.py via --path.",
            "--source",
            "PR review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "@reviewer",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "ci",
            "--layer-reason",
            "Needs base and head diff.",
        )
        self.assertEqual(rec_proc.returncode, 0)
        new_id = sorted(p.parent.name for p in (repo / ".make-it-lint/ledger").glob("F-*/record.yaml"))[-1]
        collision = run_mil(
            "scaffold",
            new_id,
            "--path",
            ".make-it-lint/checks/scope_paths.py",
            "--root",
            str(repo),
        )
        self.assertEqual(collision.returncode, 2)
        self.assertIn("already exists", collision.stderr)

        outside = run_mil("scaffold", new_id, "--path", "../escape.py", "--root", str(repo))
        self.assertEqual(outside.returncode, 2)
        self.assertIn(".make-it-lint/checks/", outside.stderr)
