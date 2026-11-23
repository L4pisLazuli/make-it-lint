from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mil.checks import CheckError, _program_argv, check_executable_issue, invoke_check
from mil.ledger import load_record_file, save_record_file
from tests.support import ROOT, SAMPLE, copy_tree, run_mil

GENERIC = ROOT / "tests/fixtures/generic-origin"
CHECKS = ".make-it-lint/checks"
LEDGER = ".make-it-lint/ledger"


def weaken(path, body: str) -> None:
    """Keep a check's metadata header and replace everything after the closing fence."""
    text = path.read_text(encoding="utf-8")
    fences = 0
    out = []
    for line in text.splitlines(keepends=True):
        out.append(line)
        if line.startswith("# ---"):
            fences += 1
            if fences == 2:
                break
    path.write_text("".join(out) + body, encoding="utf-8")
    os.chmod(path, 0o755)


class ProveTests(unittest.TestCase):
    def test_generic_origin_fixtures(self):
        proc = run_mil("lint", "--root", str(GENERIC))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run_mil("prove", "--all", "--root", str(GENERIC))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MIL-DRAFT-001", proc.stdout)
        self.assertIn("red-on-bad, green-on-good", proc.stdout)

    def test_suite_runner_and_action_hook_prove(self):
        proc = run_mil("prove", "F-20260518-01", "F-20260905-01", "--dry-run", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MIL-SUITE-001", proc.stdout)
        self.assertIn("MIL-HOOK-001", proc.stdout)

    def test_generated_catalog_is_red_on_bad_only(self):
        proc = run_mil("prove", "F-20260609-01", "--dry-run", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_prove_missing_id(self):
        proc = run_mil("prove", "F-19990101-01", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not found", proc.stderr)

    def test_prove_does_not_change_last_result_when_already_green(self):
        repo = copy_tree(SAMPLE)
        before = (repo / ".make-it-lint/ledger/F-20260311-01/record.yaml").read_text(encoding="utf-8")
        proc = run_mil("prove", "F-20260311-01", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = (repo / ".make-it-lint/ledger/F-20260311-01/record.yaml").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_all_samples_prove_and_verify_recorded(self):
        proc = run_mil("prove", "--all", "--dry-run", "--verify-recorded", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("6 of 6 proved", proc.stdout)
        self.assertNotIn("F-20260722-01", proc.stdout)

    def test_verify_recorded_catches_stale_last_result(self):
        repo = copy_tree(SAMPLE)
        rec_path = repo / LEDGER / "F-20260814-01" / "record.yaml"
        rec = load_record_file(rec_path)
        rec["proof"]["last_result"] = "red-on-bad 1/1, green-on-good 1/1"
        save_record_file(rec_path, rec)
        proc = run_mil("prove", "--all", "--dry-run", "--verify-recorded", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("recorded proof.last_result", proc.stdout)

    def test_missing_check_metadata_fails_prove(self):
        repo = copy_tree(SAMPLE)
        check = repo / CHECKS / "scope_paths.py"
        check.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
        os.chmod(check, 0o755)
        proc = run_mil("prove", "F-20260311-01", "--dry-run", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("no YAML metadata in the leading comment", proc.stdout)

    def test_writes_last_result_and_dry_run_skips_write(self):
        repo = copy_tree(SAMPLE)
        rec = repo / LEDGER / "F-20260518-01" / "record.yaml"
        rec.write_text(
            rec.read_text(encoding="utf-8").replace(
                "last_result: red-on-bad, green-on-good", "last_result: null"
            ),
            encoding="utf-8",
        )
        before = rec.read_text(encoding="utf-8")
        dry = run_mil("prove", "F-20260518-01", "--dry-run", "--root", str(repo))
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(rec.read_text(encoding="utf-8"), before)

        live = run_mil("prove", "F-20260518-01", "--root", str(repo))
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
        self.assertIn("red-on-bad, green-on-good", rec.read_text(encoding="utf-8"))

    def test_check_that_never_fires_fails(self):
        repo = copy_tree(SAMPLE)
        weaken(repo / CHECKS / "scope_paths.py", "import sys\nsys.exit(0)\n")
        proc = run_mil("prove", "F-20260311-01", "--root", str(repo))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("green-on-bad, green-on-good", proc.stdout)
        text = (repo / LEDGER / "F-20260311-01" / "record.yaml").read_text(encoding="utf-8")
        self.assertIn("green-on-bad, green-on-good", text)
        lint = run_mil("lint", "--root", str(repo))
        self.assertEqual(lint.returncode, 1, lint.stdout)
        self.assertIn("needs proof.last_result", lint.stdout)

    def test_failure_verdict_is_printed_once_with_check_stderr(self):
        repo = copy_tree(SAMPLE)
        weaken(
            repo / CHECKS / "scope_paths.py",
            "import sys\nprint('debug from scope_paths', file=sys.stderr)\nsys.exit(0)\n",
        )
        proc = run_mil("prove", "--dry-run", "F-20260311-01", "--root", str(repo))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.count("the check did not catch the failure"), 1, proc.stdout)
        self.assertNotIn("bad| bad:", proc.stdout)
        self.assertIn("bad| debug from scope_paths", proc.stdout)

    def test_check_that_always_fires_fails(self):
        repo = copy_tree(SAMPLE)
        weaken(repo / CHECKS / "suite_exit" / "check.py", 'print("run_suite.py:1: always fails")\nraise SystemExit(1)\n')
        proc = run_mil("prove", "F-20260518-01", "--root", str(repo))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("red-on-bad, red-on-good", proc.stdout)

    def test_exit_1_without_hit_lines_and_badly_formatted_hits_fail(self):
        repo = copy_tree(SAMPLE)
        weaken(repo / CHECKS / "scope_paths.py", "import sys\nsys.exit(1)\n")
        proc = run_mil("prove", "F-20260311-01", "--dry-run", "--root", str(repo))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("no hit printed", proc.stdout)

        weaken(repo / CHECKS / "scope_paths.py", "print('something is wrong')\nraise SystemExit(1)\n")
        proc2 = run_mil("prove", "F-20260311-01", "--dry-run", "--root", str(repo))
        self.assertNotEqual(proc2.returncode, 0, proc2.stdout)
        self.assertIn("not in '<path>[:<line>]: <message>'", proc2.stdout)

    def test_broken_check_exit_2_and_not_executable_fail(self):
        repo = copy_tree(SAMPLE)
        weaken(repo / CHECKS / "generated_in_pr.py", "import sys\nsys.exit(2)\n")
        proc = run_mil("prove", "F-20260609-01", "--dry-run", "--root", str(repo))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("error-on-bad, error-on-good", proc.stdout)

        (repo / CHECKS / "scope_paths.py").chmod(0o644)
        proc2 = run_mil("prove", "F-20260311-01", "--dry-run", "--root", str(repo))
        self.assertNotEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertIn("not executable", proc2.stdout + proc2.stderr)

    def test_open_without_check_is_not_provable(self):
        proc = run_mil("prove", "F-20260722-01", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("nothing to prove", proc.stdout)

    def test_invalid_id_and_missing_all_rejected(self):
        proc = run_mil("prove", "nope", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a record ID", proc.stderr)

        proc2 = run_mil("prove", "--root", str(SAMPLE))
        self.assertEqual(proc2.returncode, 2)
        self.assertIn("--all", proc2.stderr)

    def test_recorded_result_matches_a_live_proof(self):
        repo = copy_tree(SAMPLE)
        before = {p: p.read_bytes() for p in sorted((repo / LEDGER).glob("F-*/record.yaml"))}
        proc = run_mil("prove", "--all", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for path, body in before.items():
            self.assertEqual(path.read_bytes(), body, path.parent.name)

    def test_fixture_run_ignores_caller_pr_vars_and_exports_json_config(self):
        tmp = Path(tempfile.mkdtemp(prefix="mil-env-"))
        fixture = tmp / "bad"
        fixture.mkdir()
        (fixture / "pr.yaml").write_text("branch: collect/weekly\n", encoding="utf-8")
        check = tmp / "show_env.py"
        check.write_text(
            "import os\n"
            "for name in ('MIL_PR_BRANCH', 'MIL_PR_LABELS', 'MIL_PR_KIND', 'MIL_CHECK_CONFIG_JSON'):\n"
            "    print('%s=%s' % (name, os.environ.get(name, '<unset>')))\n",
            encoding="utf-8",
        )
        outer = {"MIL_PR_BRANCH": "fix/outer", "MIL_PR_LABELS": "fix", "MIL_PR_KIND": "fix"}
        with mock.patch.dict(os.environ, outer):
            proc = invoke_check(
                check,
                {"id": "MIL-DEMO-001", "input": "tree"},
                root=tmp,
                config=tmp / "config.yaml",
                fixture_dir=fixture,
                config_data={"checks": {"MIL-DEMO-001": {"timeout_seconds": 30}}},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MIL_PR_BRANCH=collect/weekly", proc.stdout)
        self.assertIn("MIL_PR_KIND=<unset>", proc.stdout)
        self.assertIn('MIL_CHECK_CONFIG_JSON={"timeout_seconds":30}', proc.stdout)

    def test_prove_advances_open_to_proven_and_keeps_shadow(self):
        repo = copy_tree(SAMPLE)
        rec_path = repo / LEDGER / "F-20260518-01" / "record.yaml"
        rec = load_record_file(rec_path)
        rec["history"] = [{"state": "open", "at": "2026-05-18"}]
        rec["enforcement"]["state"] = "open"
        rec["proof"]["last_result"] = None
        save_record_file(rec_path, rec)

        dry = run_mil("prove", "F-20260518-01", "--dry-run", "--root", str(repo))
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(load_record_file(rec_path)["enforcement"]["state"], "open")

        proc = run_mil("prove", "F-20260518-01", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = load_record_file(rec_path)
        self.assertEqual(rec["enforcement"]["state"], "proven")
        self.assertEqual(rec["proof"]["last_result"], "red-on-bad, green-on-good")
        self.assertEqual([h["state"] for h in rec["history"]], ["open", "proven"])

        shadow_path = repo / LEDGER / "F-20260311-01" / "record.yaml"
        before_shadow = load_record_file(shadow_path)
        self.assertEqual(run_mil("prove", "F-20260311-01", "--root", str(repo)).returncode, 0)
        self.assertEqual(load_record_file(shadow_path)["history"], before_shadow["history"])

    def test_windows_dispatch_and_git_exec_bit_validation(self):
        import sys

        tmp = Path(tempfile.mkdtemp(prefix="mil-win-"))
        py_no_shebang = tmp / "plain_rule.py"
        py_no_shebang.write_text("raise SystemExit(0)\n", encoding="utf-8")
        sh_check = tmp / "posix_rule.sh"
        sh_check.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        bare_file = tmp / "untyped_rule"
        bare_file.write_text("id: MIL-RAW-001\n", encoding="utf-8")

        with mock.patch("mil.checks.os.name", "nt"):
            self.assertEqual(_program_argv(py_no_shebang), [sys.executable, str(py_no_shebang)])
            with mock.patch("mil.checks.shutil.which", return_value=r"C:\Program Files\Git\bin\sh.exe"):
                self.assertEqual(
                    _program_argv(sh_check),
                    [r"C:\Program Files\Git\bin\sh.exe", str(sh_check)],
                )
            with mock.patch("mil.checks.shutil.which", return_value=None):
                with self.assertRaises(CheckError):
                    _program_argv(sh_check)

            self.assertIsNone(check_executable_issue(tmp, py_no_shebang, git_non_exec=set()))
            self.assertIn(
                "tracked in git without +x",
                check_executable_issue(tmp, py_no_shebang, git_non_exec={py_no_shebang.resolve()}) or "",
            )
            self.assertIn(
                "not executable on Windows",
                check_executable_issue(tmp, bare_file, git_non_exec=set()) or "",
            )
