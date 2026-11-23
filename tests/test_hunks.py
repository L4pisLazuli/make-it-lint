"""Tests for hunk-revert proof verification (`mil prove --check-hunks`)."""

from __future__ import annotations

import os
import unittest

from tests.support import run_mil, scratch_adopter

NARROW_CHECK = '''#!/usr/bin/env python3
# ---
# id: MIL-HUNK-001
# incident: {incident}
# input: tree
# layer: lint
# tool: script
# ---
"""Only checks the first timeout key and misses retry_limit."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--root")
parser.add_argument("--config")
args = parser.parse_args()
root = Path(args.root)
cfg = root / "config/runner.yaml"
if cfg.is_file() and "timeout_seconds: 0" in cfg.read_text(encoding="utf-8"):
    print("config/runner.yaml:1: timeout_seconds must be positive")
    raise SystemExit(1)
raise SystemExit(0)
'''


class HunkRevertTests(unittest.TestCase):
    def test_check_hunks_warns_on_missed_code_hunk_and_skips_comments(self):
        root = scratch_adopter()
        bad = root / "bad_src"
        good = root / "good_src"
        (bad / "config").mkdir(parents=True)
        (good / "config").mkdir(parents=True)
        (bad / "config/runner.yaml").write_text(
            "timeout_seconds: 0\n"
            "worker_pool: warehouse-east\n"
            "queue_name: pick-lists\n"
            "batch_size: 50\n"
            "flush_interval_ms: 250\n"
            "max_inflight: 8\n"
            "backoff_ms: 500\n"
            "retry_limit: -1\n",
            encoding="utf-8",
        )
        (good / "config/runner.yaml").write_text(
            "timeout_seconds: 30\n"
            "worker_pool: warehouse-east\n"
            "queue_name: pick-lists\n"
            "batch_size: 50\n"
            "flush_interval_ms: 250\n"
            "max_inflight: 8\n"
            "backoff_ms: 500\n"
            "retry_limit: 3\n",
            encoding="utf-8",
        )
        # Also include a Python file where only a comment changed between bad/ and good/
        (bad / "runner_notes.py").write_text(
            "# old runner note\ndef pool_name():\n    return 'warehouse-east'\n",
            encoding="utf-8",
        )
        (good / "runner_notes.py").write_text(
            "# updated runner note\ndef pool_name():\n    return 'warehouse-east'\n",
            encoding="utf-8",
        )

        rec_proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Runner config allowed zero timeout and negative retry limit",
            "--fingerprint",
            "data:invalid-runner-limits",
            "--source",
            "staging outage",
            "--actor",
            "cloud-agent",
            "--reporter",
            "developer",
            "--what",
            "Both timeout_seconds=0 and retry_limit=-1 were committed.",
            "--kind",
            "data",
            "--decidable",
            "yes",
            "--layer",
            "lint",
            "--layer-reason",
            "Static bounds in config/runner.yaml.",
            "--bad",
            str(bad),
            "--good",
            str(good),
        )
        self.assertEqual(rec_proc.returncode, 0, rec_proc.stdout + rec_proc.stderr)
        rid = next((root / ".make-it-lint/ledger").glob("F-*")).name
        check = root / ".make-it-lint/checks/runner_limits.py"
        check.write_text(NARROW_CHECK.format(incident=rid), encoding="utf-8")
        os.chmod(check, 0o755)
        self.assertEqual(run_mil("link", rid, ".make-it-lint/checks/runner_limits.py", "--root", str(root)).returncode, 0)

        prove = run_mil("prove", rid, "--check-hunks", "--root", str(root))
        self.assertEqual(prove.returncode, 0, prove.stdout + prove.stderr)
        # Must warn on the unguarded retry_limit hunk in config/runner.yaml:
        self.assertIn("warn: hunk revert stayed green on good config/runner.yaml", prove.stdout)
        # Must NOT warn on the comment-only hunk in runner_notes.py:
        self.assertNotIn("runner_notes.py", prove.stdout)
