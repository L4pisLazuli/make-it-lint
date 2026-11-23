from __future__ import annotations

import json
import unittest

from tests.support import SAMPLE, copy_tree, run_mil, scratch_adopter
from tests.test_ci import _add_check


class EventsTests(unittest.TestCase):
    def test_ci_separates_shadow_runs_from_shadow_hits(self):
        root = scratch_adopter()
        _add_check(root, "MIL-CI-SHADOW", "shadow")
        events = root / "mil-events.ndjson"
        # First run: clean tree -> 1 shadow_run, 0 hits
        clean_run = run_mil(
            "ci",
            "--root",
            str(root),
            "--events",
            str(events),
            env={"GITHUB_ACTIONS": ""},
        )
        self.assertEqual(clean_run.returncode, 0, clean_run.stdout + clean_run.stderr)
        # Second run: hitting tree -> +1 shadow_run, +1 shadow_hit
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        hit_run = run_mil(
            "ci",
            "--root",
            str(root),
            "--events",
            str(events),
            env={"GITHUB_ACTIONS": ""},
        )
        self.assertEqual(hit_run.returncode, 0, hit_run.stdout + hit_run.stderr)
        rows = [json.loads(ln) for ln in events.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertTrue(all(r.get("ts", "").endswith("Z") for r in rows))
        types = [r.get("type") for r in rows]
        self.assertEqual(types.count("shadow_run"), 2)
        self.assertEqual(types.count("shadow_hit"), 1)
        summary = run_mil("events", "--summary", str(events), "--root", str(root))
        self.assertEqual(summary.returncode, 0, summary.stdout + summary.stderr)
        self.assertIn("check  shadow_runs  hits  waivers  errors  proves", summary.stdout)
        self.assertIn("MIL-CI-SHADOW  2  1  0  0  0", summary.stdout)

    def test_summary_aggregates_directory_of_artifacts(self):
        root = scratch_adopter()
        _add_check(root, "MIL-CI-SHADOW", "shadow")
        artifacts = root / "ci-artifacts"
        (artifacts / "run-101").mkdir(parents=True)
        (artifacts / "run-102").mkdir(parents=True)
        run_mil("ci", "--root", str(root), "--events", str(artifacts / "run-101" / "events.ndjson"))
        run_mil("ci", "--root", str(root), "--events", str(artifacts / "run-102" / "events.ndjson"))
        summary = run_mil("events", "--summary", str(artifacts), "--root", str(root))
        self.assertEqual(summary.returncode, 0, summary.stdout + summary.stderr)
        self.assertIn("MIL-CI-SHADOW  2  0  0  0  0", summary.stdout)

    def test_events_commit_writes_outside_ledger(self):
        root = scratch_adopter()
        cfg = root / ".make-it-lint/config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + "events:\n  commit: true\n", encoding="utf-8")
        _add_check(root, "MIL-CI-SHADOW", "shadow")
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        proc = run_mil("ci", "--root", str(root), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        committed = root / ".make-it-lint/events.ndjson"
        self.assertTrue(committed.is_file())
        self.assertFalse((root / ".make-it-lint/ledger/events.ndjson").exists())
        text = committed.read_text(encoding="utf-8")
        self.assertIn("shadow_run", text)
        self.assertIn("shadow_hit", text)

    def test_events_refuse_ledger_path(self):
        root = scratch_adopter()
        _add_check(root, "MIL-CI-SHADOW", "shadow")
        dest = root / ".make-it-lint/ledger/events.ndjson"
        proc = run_mil(
            "ci",
            "--root",
            str(root),
            "--events",
            str(dest),
            env={"GITHUB_ACTIONS": ""},
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("ledger", proc.stderr)
        self.assertFalse(dest.exists())

    def test_summary_without_file_is_empty(self):
        root = scratch_adopter()
        proc = run_mil("events", "--summary", "--root", str(root))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no events", proc.stdout)

    def test_waiver_event_is_recorded(self):
        root = scratch_adopter()
        _add_check(root, "MIL-CI-BLOCK", "block")
        (root / "forbidden.yaml").write_text("token: leaked-collect-timeout\n", encoding="utf-8")
        (root / ".make-it-lint/waivers.yaml").write_text(
            "- check: MIL-CI-BLOCK\n"
            "  paths: [forbidden.yaml]\n"
            "  reason: one-line timeout had to land with the collect extract\n"
            "  approved_by: \"@ledger-owner\"\n"
            "  expires: 2027-03-01\n",
            encoding="utf-8",
        )
        events = root / "mil-events.ndjson"
        proc = run_mil("ci", "--root", str(root), "--events", str(events), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = events.read_text(encoding="utf-8")
        self.assertIn('"type":"waiver"', text)
        summary = run_mil("events", "--summary", str(events), "--root", str(root))
        self.assertIn("MIL-CI-BLOCK  0  1  1  0  0", summary.stdout)

    def test_events_file_and_commit_counts_match(self):
        repo = copy_tree(SAMPLE)
        cfg = repo / ".make-it-lint/config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + "events:\n  commit: true\n", encoding="utf-8")
        extra = repo / "mil-events.ndjson"
        prove = run_mil("prove", "--all", "--events", str(extra), "--root", str(repo))
        self.assertEqual(prove.returncode, 0, prove.stdout + prove.stderr)
        ci = run_mil("ci", "--events", str(extra), "--root", str(repo), env={"GITHUB_ACTIONS": ""})
        self.assertEqual(ci.returncode, 0, ci.stdout + ci.stderr)
        from_file = run_mil("events", "--summary", str(extra), "--root", str(repo))
        from_commit = run_mil("events", "--summary", "--root", str(repo))
        self.assertEqual(from_file.returncode, 0, from_file.stdout)
        self.assertEqual(from_commit.returncode, 0, from_commit.stdout)
        self.assertEqual(from_file.stdout, from_commit.stdout)
        self.assertNotEqual(from_file.stdout.strip(), "mil events: no events")
