from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ROOT, run_mil, scratch_adopter

HIT_CHECK = '''#!/usr/bin/env python3
# ---
# id: {check_id}
# origin: sample-harness/forbidden-config
# input: tree
# enforcement:
#   state: {state}
# ---
"""Fail when forbidden.yaml is in the product tree."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--root")
parser.add_argument("--config")
args = parser.parse_args()
target = Path(args.root) / "forbidden.yaml"
if target.is_file():
    print("forbidden.yaml: drop forbidden.yaml from the product tree")
    raise SystemExit(1)
raise SystemExit(0)
'''


def _add_check(root: Path, check_id: str, state: str) -> Path:
    path = root / ".make-it-lint/checks" / f"{check_id.lower().replace('-', '_')}.py"
    path.write_text(HIT_CHECK.format(check_id=check_id, state=state), encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _load_pr_comment_module():
    spec = importlib.util.spec_from_file_location(
        "pr_comment", ROOT / ".github/scripts/pr-comment.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CiTests(unittest.TestCase):
    def test_shadow_hit_does_not_fail(self):
        root = scratch_adopter()
        _add_check(root, "MIL-CI-SHADOW", "shadow")
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("forbidden.yaml", proc.stdout)
        self.assertIn("shadow", proc.stdout)
        self.assertIn("1 shadow hit", proc.stdout)

    def test_block_hit_fails(self):
        root = scratch_adopter()
        _add_check(root, "MIL-CI-BLOCK", "block")
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("1 block hit", proc.stdout)

    def test_waiver_downgrades_block_to_comment_including_dotfiles_and_globs(self):
        root = scratch_adopter()
        check = root / ".make-it-lint/checks/workflow_pin.py"
        check.write_text(
            "#!/usr/bin/env python3\n"
            "# ---\n"
            "# id: MIL-WF-PIN\n"
            "# origin: sample-harness/workflow-pin\n"
            "# input: tree\n"
            "# enforcement:\n"
            "#   state: block\n"
            "# ---\n"
            "import argparse\n"
            "from pathlib import Path\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--root')\n"
            "parser.add_argument('--config')\n"
            "args = parser.parse_args()\n"
            "wf = Path(args.root) / '.github/workflows/release.yml'\n"
            "if wf.is_file():\n"
            "    print('.github/workflows/release.yml:8: pin action to commit SHA')\n"
            "    raise SystemExit(1)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        os.chmod(check, 0o755)
        wf = root / ".github/workflows/release.yml"
        wf.parent.mkdir(parents=True)
        wf.write_text("uses: actions/checkout@v4\n", encoding="utf-8")
        (root / ".make-it-lint/waivers.yaml").write_text(
            "- check: MIL-WF-PIN\n"
            "  paths: ['.github/workflows/*.yml']\n"
            "  reason: release workflow migrates to pinned SHA in next cut\n"
            "  approved_by: \"@ledger-owner\"\n"
            "  expires: 2027-03-01\n",
            encoding="utf-8",
        )
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("1 comment hit", proc.stdout)
        self.assertIn("0 block hit", proc.stdout)

    def test_orphan_incident_check_fails_ci_instead_of_falling_back_to_shadow(self):
        root = scratch_adopter()
        orphan = root / ".make-it-lint/checks/orphan_incident.py"
        orphan.write_text(
            "#!/usr/bin/env python3\n"
            "# ---\n"
            "# id: MIL-ORPHAN-INC\n"
            "# incident: F-20260311-99\n"
            "# input: tree\n"
            "# ---\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        os.chmod(orphan, 0o755)
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("has no record in .make-it-lint/ledger/", proc.stdout)

    def test_github_annotations_for_comment(self):
        root = scratch_adopter()
        _add_check(root, "MIL-CI-COMMENT", "comment")
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": "true"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::warning", proc.stdout)
        self.assertIn("file=forbidden.yaml", proc.stdout)

    def test_json_summary_and_pr_comment_silence_on_shadow_only(self):
        mod = _load_pr_comment_module()
        root = scratch_adopter()
        _add_check(root, "MIL-CI-SHADOW", "shadow")
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        shadow_summary = root / "shadow-summary.json"
        proc = run_mil("ci", "--root", str(root), "--json-summary", str(shadow_summary), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        shadow_data = json.loads(shadow_summary.read_text(encoding="utf-8"))
        self.assertFalse(mod.should_post_new_comment(shadow_data))

        # Now add a comment-level check -> should_post_new_comment becomes True
        _add_check(root, "MIL-CI-COMMENT", "comment")
        comment_summary = root / "comment-summary.json"
        proc2 = run_mil("ci", "--root", str(root), "--json-summary", str(comment_summary), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        comment_data = json.loads(comment_summary.read_text(encoding="utf-8"))
        self.assertTrue(mod.should_post_new_comment(comment_data))
        body = mod.render_comment(comment_data)
        self.assertIn("<!-- mil-ci -->", body)
        self.assertIn("MIL-CI-COMMENT", body)
        self.assertIn("forbidden.yaml", body)

    def test_pr_comment_pagination_finds_second_page_marker(self):
        mod = _load_pr_comment_module()
        page_1 = [{"id": 100 + i, "body": f"Review note #{i}"} for i in range(100)]
        page_2 = [{"id": 999, "body": "<!-- mil-ci -->\n## make-it-lint\n"}]
        calls: list[tuple[str, str]] = []

        def fake_request(method: str, url: str, token: str, data: bytes | None = None):
            calls.append((method, url))
            if method == "GET" and url.endswith("&page=1"):
                return page_1
            if method == "GET" and url.endswith("&page=2"):
                return page_2
            return {"id": 999}

        with mock.patch.object(mod, "_request", side_effect=fake_request):
            found = mod._find_existing_comment("https://api.github.com", "org/repo", "42", "tok")
            self.assertEqual(found, 999)
            mod._upsert_comment("https://api.github.com", "org/repo", "42", "tok", "updated", existing_id=found)
        self.assertTrue(any(m == "PATCH" and url.endswith("/comments/999") for m, url in calls))

    def test_summary_includes_evidence_and_seed(self):
        from mil.ledger import load_record_file, save_record_file

        root = scratch_adopter()
        _add_check(root, "MIL-CI-COMMENT", "comment")
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Collect tree shipped forbidden.yaml",
            "--fingerprint",
            "scope:forbidden-yaml",
            "--source",
            "review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "reviewer",
            "--what",
            "Collect is limited to inventory/; forbidden.yaml was present.",
            "--kind",
            "scope",
            "--decidable",
            "yes",
            "--layer",
            "lint",
            "--layer-reason",
            "The forbidden file is visible in the tree.",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        rec = load_record_file(rec_path)
        rec["proof"] = {"bad": "bad/", "good": "good/", "last_result": "red-on-bad, green-on-good", "seed": 7}
        rec["evidence"] = [
            {
                "kind": "log",
                "url": "https://ci.example.invalid/runs/2026-03/trace-%d" % i,
                "note": "collect pipeline shard %d" % i,
            }
            for i in range(1, 5)
        ]
        check = root / ".make-it-lint/checks/mil_ci_comment.py"
        text = check.read_text(encoding="utf-8")
        check.write_text(
            text.replace("origin: sample-harness/forbidden-config", "incident: %s" % rec["id"]).replace(
                "# enforcement:\n#   state: comment\n", ""
            ),
            encoding="utf-8",
        )
        rec["check"] = {
            "id": "MIL-CI-COMMENT",
            "tool": "script",
            "path": ".make-it-lint/checks/mil_ci_comment.py",
            "message": "drop forbidden.yaml",
        }
        rec["enforcement"]["state"] = "comment"
        rec["history"] = [
            {"state": "open", "at": "2026-03-11"},
            {"state": "proven", "at": "2026-03-13", "note": "mil prove"},
            {"state": "shadow", "at": "2026-03-14", "by": "@ledger-owner"},
            {"state": "comment", "at": "2026-03-15", "by": "@ledger-owner"},
        ]
        (rec_path.parent / "bad").mkdir(exist_ok=True)
        (rec_path.parent / "good").mkdir(exist_ok=True)
        (rec_path.parent / "bad" / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        (rec_path.parent / "good" / "inventory.yaml").write_text("sku: WIDGET-04\nqty: 12\n", encoding="utf-8")
        save_record_file(rec_path, rec)

        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

        summary = root / "mil-ci-summary.json"
        proc = run_mil(
            "ci",
            "--root",
            str(root),
            "--json-summary",
            str(summary),
            env={"GITHUB_ACTIONS": ""},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("seed: 7", proc.stdout)
        self.assertIn("evidence:", proc.stdout)
        self.assertIn("(+1)", proc.stdout)
        data = json.loads(summary.read_text(encoding="utf-8"))
        job = next(j for j in data["ran"] if j["id"] == "MIL-CI-COMMENT")
        self.assertEqual(job["seed"], 7)
        self.assertEqual(len(job["evidence"]), 4)

    def test_skips_diff_checks_without_base_and_skips_action_checks(self):
        root = scratch_adopter()
        diff_check = root / ".make-it-lint/checks/diff_only.py"
        diff_check.write_text(
            "#!/usr/bin/env python3\n"
            "# ---\n"
            "# id: MIL-CI-DIFF\n"
            "# origin: sample-harness/diff-only\n"
            "# input: diff\n"
            "# enforcement:\n"
            "#   state: block\n"
            "# ---\n"
            'raise SystemExit("should not run")\n',
            encoding="utf-8",
        )
        os.chmod(diff_check, 0o755)
        action_check = root / ".make-it-lint/checks/action_only.py"
        action_check.write_text(
            "#!/usr/bin/env python3\n"
            "# ---\n"
            "# id: MIL-CI-ACTION\n"
            "# origin: sample-harness/action-only\n"
            "# input: action\n"
            "# enforcement:\n"
            "#   state: block\n"
            "# ---\n"
            'raise SystemExit("should not run")\n',
            encoding="utf-8",
        )
        os.chmod(action_check, 0o755)
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("diff check needs --base and --head", proc.stdout)
        self.assertIn("action check runs in mil guard", proc.stdout)

    def test_skips_proven_and_earlier(self):
        root = scratch_adopter()
        _add_check(root, "MIL-CI-PROVEN", "proven")
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("not run against product code", proc.stdout)

    def test_pr_comment_script_skips_without_token(self):
        mod = _load_pr_comment_module()
        code = mod.main(["/no-such-summary.json"])
        self.assertEqual(code, 0)
