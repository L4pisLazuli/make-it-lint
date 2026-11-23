"""Tests for mil guard, PreToolUse hook, protected paths, and git-materialized mil ci."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from mil.ledger import load_record_file, save_record_file
from tests.support import ROOT, SAMPLE, copy_tree, run_mil, run_mil_tty

PRE_TOOL_HOOK = ROOT / "skills/make-it-lint/hooks/pre_tool_guard.py"


class GuardAndMaterializedCiTests(unittest.TestCase):
    def _run_hook(self, repo: Path, payload: dict) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = str(repo)
        return subprocess.run(
            [sys.executable, str(PRE_TOOL_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_pre_tool_guard_blocks_protected_edits_and_allows_new_check(self):
        repo = copy_tree(SAMPLE)
        # Create a symlink/copy of lib/mil so pre_tool_guard can run self-contained in repo
        import shutil

        shutil.copytree(ROOT / "lib", repo / "lib")

        # 1) Direct Edit to waivers.yaml -> DENIED (Claude Code PreToolUse format)
        deny_waiver = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / ".make-it-lint/waivers.yaml")},
                "cwd": str(repo),
            },
        )
        self.assertEqual(deny_waiver.returncode, 0, deny_waiver.stderr)
        out = json.loads(deny_waiver.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("waivers.yaml is human-only", out["hookSpecificOutput"]["permissionDecisionReason"])

        # 2) Direct Write to an EXISTING check -> DENIED (Cursor preToolUse format)
        deny_existing_check = self._run_hook(
            repo,
            {
                "hook_event_name": "preToolUse",
                "tool_name": "StrReplace",
                "workspace_roots": [str(repo)],
                "tool_input": {"path": ".make-it-lint/checks/scope_paths.py"},
            },
        )
        self.assertEqual(deny_existing_check.returncode, 0)
        cursor_out = json.loads(deny_existing_check.stdout)
        self.assertEqual(cursor_out["permission"], "deny")
        self.assertIn("already exists", cursor_out["agent_message"])

        # 3) Direct Write to an EXISTING record.yaml -> DENIED
        deny_record = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": ".make-it-lint/ledger/F-20260311-01/record.yaml"},
                "cwd": str(repo),
            },
        )
        self.assertEqual(
            json.loads(deny_record.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        # 4) Writing a BRAND NEW check file -> ALLOWED (empty stdout)
        allow_new_check = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": ".make-it-lint/checks/new_inventory_rule.py"},
                "cwd": str(repo),
            },
        )
        self.assertEqual(allow_new_check.returncode, 0)
        self.assertEqual(allow_new_check.stdout.strip(), "")

        # 5) Running live `mil promote` via Bash -> DENIED (MIL-GUARD-PROMOTE)
        deny_promote = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "bin/mil promote F-20260402-01 --approved-by @ledger-owner"},
                "cwd": str(repo),
            },
        )
        self.assertEqual(deny_promote.returncode, 0)
        promote_out = json.loads(deny_promote.stdout)["hookSpecificOutput"]
        self.assertEqual(promote_out["permissionDecision"], "deny")
        self.assertIn("MIL-GUARD-PROMOTE", promote_out["permissionDecisionReason"])

        # 6) Running `mil promote --dry-run` via Bash -> ALLOWED, and commit messages mentioning
        #    `mil promote` are not falsely flagged, while quoted/chained/--note "--dry-run" forms are DENIED!
        for allowed_cmd in (
            "bin/mil promote F-20260402-01 --approved-by @ledger-owner --dry-run",
            'git commit -m "Require human terminal confirmation before mil promote"',
        ):
            allow_proc = self._run_hook(
                repo,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": allowed_cmd},
                    "cwd": str(repo),
                },
            )
            self.assertEqual(allow_proc.returncode, 0)
            self.assertEqual(allow_proc.stdout.strip(), "", allowed_cmd)

        for blocked_cmd in (
            '"$CLAUDE_PROJECT_DIR/bin/mil" promote F-20260402-01 --approved-by @ledger-owner',
            'py -3 -m mil --root . promote F-20260402-01 --approved-by @ledger-owner --note "checked --dry-run"',
            "bin/mil promote F-20260402-01 --approved-by @ledger-owner --dry-run && bin/mil promote F-20260402-01 --approved-by @ledger-owner",
        ):
            blocked_proc = self._run_hook(
                repo,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": blocked_cmd},
                    "cwd": str(repo),
                },
            )
            blocked_out = json.loads(blocked_proc.stdout)["hookSpecificOutput"]
            self.assertEqual(blocked_out["permissionDecision"], "deny", blocked_cmd)
            self.assertIn("MIL-GUARD-PROMOTE", blocked_out["permissionDecisionReason"])

        # 7) Japanese Windows cp932 stdio simulation: UTF-8 hook payload containing non-cp932
        #    code points (`──▶`) must still deny protected writes instead of failing open!
        cp932_env = os.environ.copy()
        cp932_env["CLAUDE_PROJECT_DIR"] = str(repo)
        cp932_env["PYTHONIOENCODING"] = "cp932"
        utf8_payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {
                    "file_path": ".make-it-lint/waivers.yaml",
                    "content": "# 表示 ──▶ 構築\n[]\n",
                },
                "cwd": str(repo),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        cp932_proc = subprocess.run(
            [sys.executable, str(PRE_TOOL_HOOK)],
            input=utf8_payload,
            capture_output=True,
            env=cp932_env,
        )
        self.assertEqual(cp932_proc.returncode, 0)
        cp932_out = json.loads(cp932_proc.stdout.decode("utf-8"))
        self.assertEqual(cp932_out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_action_check_enforced_by_mil_guard_in_comment_and_block(self):
        repo = copy_tree(SAMPLE)
        import shutil

        shutil.copytree(ROOT / "lib", repo / "lib")

        # Promote F-20260905-01 (MIL-HOOK-001) proven -> shadow -> comment -> block
        for step in ("shadow", "comment"):
            self.assertEqual(
                run_mil_tty(
                    f"{step}\n",
                    "promote",
                    "F-20260905-01",
                    "--to",
                    step,
                    "--approved-by",
                    "@ledger-owner",
                    "--root",
                    str(repo),
                ).returncode,
                0,
            )

        # At `comment`, force-pushing to main emits additionalContext warning and allows
        warn_proc = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git push --force origin HEAD:main"},
                "cwd": str(repo),
            },
        )
        warn_out = json.loads(warn_proc.stdout)["hookSpecificOutput"]
        self.assertEqual(warn_out["permissionDecision"], "allow")
        self.assertIn("MIL-HOOK-001", warn_out["additionalContext"])

        # Promote to `block` -> denies `git push --force origin HEAD:main`!
        self.assertEqual(
            run_mil_tty(
                "block\n",
                "promote",
                "F-20260905-01",
                "--to",
                "block",
                "--approved-by",
                "@ledger-owner",
                "--root",
                str(repo),
            ).returncode,
            0,
        )
        block_proc = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git push --force origin HEAD:main"},
                "cwd": str(repo),
            },
        )
        block_out = json.loads(block_proc.stdout)["hookSpecificOutput"]
        self.assertEqual(block_out["permissionDecision"], "deny")
        self.assertIn("MIL-HOOK-001", block_out["permissionDecisionReason"])

        # Feature branch force-with-lease is allowed cleanly
        ok_proc = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git push --force-with-lease origin collect/weekly-sync"},
                "cwd": str(repo),
            },
        )
        self.assertEqual(ok_proc.stdout.strip(), "")

    def test_ci_base_ref_head_ref_ignores_untracked_events_and_catches_tampered_history(self):
        repo = copy_tree(SAMPLE)
        # Promote MIL-SCOPE-001 to block (without waiver on unauthorized path)
        for step in ("comment", "block"):
            self.assertEqual(
                run_mil_tty(
                    f"{step}\n",
                    "promote",
                    "F-20260311-01",
                    "--to",
                    step,
                    "--approved-by",
                    "@ledger-owner",
                    "--root",
                    str(repo),
                ).returncode,
                0,
            )

        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "owner@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "ledger-owner"], cwd=repo, check=True)
        (repo / "inventory").mkdir()
        (repo / "inventory/items.yaml").write_text("sku: WIDGET-04\nqty: 12\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base commit"], cwd=repo, check=True)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        # Switch to a collect/* branch and commit ONLY an allowed inventory change,
        # while writing mil-events/events.ndjson in the working directory (like CI does!).
        subprocess.run(["git", "checkout", "-q", "-b", "collect/q4-warehouse"], cwd=repo, check=True)
        (repo / "inventory/items.yaml").write_text("sku: WIDGET-04\nqty: 18\n", encoding="utf-8")
        subprocess.run(["git", "add", "inventory/items.yaml"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "update inventory qty"], cwd=repo, check=True)

        events_file = repo / "mil-events/events.ndjson"
        events_file.parent.mkdir()
        events_file.write_text("", encoding="utf-8")
        summary_file = repo / "mil-ci-summary.json"

        ci_clean = run_mil(
            "ci",
            "--root",
            str(repo),
            "--base-ref",
            base_sha,
            "--head-ref",
            "HEAD",
            "--events",
            str(events_file),
            "--json-summary",
            str(summary_file),
            env={"MIL_PR_BRANCH": "collect/q4-warehouse", "GITHUB_ACTIONS": ""},
        )
        self.assertEqual(ci_clean.returncode, 0, ci_clean.stdout + ci_clean.stderr)
        self.assertIn("0 block hit", ci_clean.stdout)

        # Now tamper with an existing record's past history entry and commit -> mil ci blocks!
        rec_path = repo / ".make-it-lint/ledger/F-20260402-01/record.yaml"
        rec = load_record_file(rec_path)
        rec["history"][0]["at"] = "2026-01-01"
        save_record_file(rec_path, rec)
        subprocess.run(["git", "add", str(rec_path)], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "tamper history"], cwd=repo, check=True)

        ci_tampered = run_mil(
            "ci",
            "--root",
            str(repo),
            "--base-ref",
            base_sha,
            "--head-ref",
            "HEAD",
            env={"MIL_PR_BRANCH": "feat/normal-branch", "GITHUB_ACTIONS": ""},
        )
        self.assertEqual(ci_tampered.returncode, 1, ci_tampered.stdout + ci_tampered.stderr)
        self.assertIn("past history entries were rewritten", ci_tampered.stdout)

    def test_open_uncommitted_draft_check_is_editable_until_proven_or_committed(self):
        repo = copy_tree(SAMPLE)
        import shutil

        shutil.copytree(ROOT / "lib", repo / "lib")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "owner@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "ledger-owner"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=repo, check=True)

        bad_fx = repo / "fx_tmp" / "bad"
        good_fx = repo / "fx_tmp" / "good"
        (bad_fx / "src").mkdir(parents=True)
        (good_fx / "src").mkdir(parents=True)
        (bad_fx / "src" / "service.py").write_text("breakpoint()\n", encoding="utf-8")
        (good_fx / "src" / "service.py").write_text("def serve():\n    return 200\n", encoding="utf-8")

        self.assertEqual(
            run_mil(
                "record",
                "--root",
                str(repo),
                "--fingerprint",
                "lint:service-breakpoint",
                "--title",
                "Left breakpoint() in service module",
                "--what",
                "Agent left breakpoint() in src/service.py.",
                "--source",
                "PR review",
                "--actor",
                "cloud-agent",
                "--reporter",
                "@ledger-owner",
                "--kind",
                "scope",
                "--decidable",
                "yes",
                "--layer",
                "lint",
                "--layer-reason",
                "Single-file AST/line inspection suffices.",
                "--bad",
                str(bad_fx),
                "--good",
                str(good_fx),
            ).returncode,
            0,
        )
        shutil.rmtree(repo / "fx_tmp")
        rid = sorted(p.parent.name for p in (repo / ".make-it-lint/ledger").glob("F-*/record.yaml"))[-1]
        self.assertEqual(run_mil("scaffold", rid, "--root", str(repo)).returncode, 0)
        draft_rel = ".make-it-lint/checks/service_breakpoint.py"
        draft_file = repo / draft_rel

        # While `open` and not in git HEAD, PreToolUse allows iterating on the draft check!
        allow_draft = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": draft_rel},
                "cwd": str(repo),
            },
        )
        self.assertEqual(allow_draft.returncode, 0, allow_draft.stderr)
        self.assertEqual(allow_draft.stdout.strip(), "")

        # Implement the rule and prove (`open -> proven`)
        body = draft_file.read_text(encoding="utf-8")
        placeholder = (
            "        # Replace the two lines below with your tree check appending '<rel_path>[:<line>]: <how to fix>' to `hits`.\n"
            "        break\n"
            f'    sys.stderr.write("{draft_rel}: unimplemented check scaffold; edit the rule logic before running mil prove\\n")\n'
            "    return 2"
        )
        rule = (
            '        if path.suffix == ".py":\n'
            '            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):\n'
            '                if line.strip().startswith("breakpoint()"):\n'
            '                    hits.append(f"{path.relative_to(root).as_posix()}:{lineno}: remove breakpoint()")'
        )
        draft_file.write_text(body.replace(placeholder, rule, 1), encoding="utf-8")
        self.assertEqual(run_mil("prove", rid, "--root", str(repo)).returncode, 0)

        # Immediately after `mil prove` (`state == proven`), PreToolUse blocks silent edits!
        deny_after_prove = self._run_hook(
            repo,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": draft_rel},
                "cwd": str(repo),
            },
        )
        self.assertEqual(
            json.loads(deny_after_prove.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
