from __future__ import annotations

import hashlib
import os
import subprocess
import unittest

from mil.ledger import load_record_file, save_record_file
from tests.support import SAMPLE, copy_tree, run_mil, scratch_adopter


class EvidenceLintTests(unittest.TestCase):
    def test_sample_evidence_lints(self):
        proc = run_mil("lint", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_wrong_sha256_fails(self):
        repo = copy_tree(SAMPLE)
        rec_path = repo / ".make-it-lint/ledger/F-20260814-01/record.yaml"
        rec = load_record_file(rec_path)
        rec["evidence"][0]["sha256"] = "0" * 64
        save_record_file(rec_path, rec)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("sha256 does not match", proc.stdout)

    def test_missing_sha256_fails(self):
        repo = copy_tree(SAMPLE)
        rec_path = repo / ".make-it-lint/ledger/F-20260814-01/record.yaml"
        rec = load_record_file(rec_path)
        rec["evidence"][0].pop("sha256", None)
        save_record_file(rec_path, rec)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("sha256", proc.stdout)

    def test_missing_evidence_path_fails(self):
        repo = copy_tree(SAMPLE)
        rec_path = repo / ".make-it-lint/ledger/F-20260814-01/record.yaml"
        rec = load_record_file(rec_path)
        rec["evidence"][0]["path"] = ".make-it-lint/ledger/F-20260814-01/evidence/missing.log"
        save_record_file(rec_path, rec)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("does not exist", proc.stdout)

    def test_traversal_outside_repo_root_fails(self):
        repo = copy_tree(SAMPLE)
        outside = repo.parent / "leaked-host-config.log"
        outside.write_text("host-level diagnostic snapshot\n", encoding="utf-8")
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        rec_path = repo / ".make-it-lint/ledger/F-20260814-01/record.yaml"
        rec = load_record_file(rec_path)
        rec["evidence"][0]["path"] = "../leaked-host-config.log"
        rec["evidence"][0]["sha256"] = digest
        save_record_file(rec_path, rec)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("escapes the repository root", proc.stdout)

    def test_matching_sha256_passes(self):
        repo = copy_tree(SAMPLE)
        path = repo / ".make-it-lint/ledger/F-20260814-01/evidence/counterexample.log"
        rec_path = repo / ".make-it-lint/ledger/F-20260814-01/record.yaml"
        rec = load_record_file(rec_path)
        rec["evidence"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        save_record_file(rec_path, rec)
        proc = run_mil("lint", "--root", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_covers_warns_when_file_changed_after_synced_commit(self):
        root = scratch_adopter()
        src = root / "tools"
        src.mkdir()
        target = src / "pack_pick.py"
        target.write_text("def pack(lines):\n    return list(lines)\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "ledger-owner@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "ledger-owner"], cwd=root, check=True)
        subprocess.run(["git", "add", "tools/pack_pick.py"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pack pick identity"], cwd=root, check=True, capture_output=True)
        first = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        proc = run_mil(
            "record",
            "--root",
            str(root),
            "--title",
            "Packer dropped a pick-list line",
            "--fingerprint",
            "data:pack-drops-line",
            "--source",
            "pick run",
            "--actor",
            "local-agent",
            "--reporter",
            "developer",
            "--what",
            "pack() dropped the last SKU.",
            "--kind",
            "data",
            "--decidable",
            "yes",
            "--layer",
            "verify",
            "--layer-reason",
            "Membership is an invariant a property check can draw.",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        rec = load_record_file(rec_path)
        rec["covers"] = {"files": ["tools/pack_pick.py"], "synced_commit": first}
        save_record_file(rec_path, rec)
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertNotIn("covers.files changed", lint.stdout)
        target.write_text("def pack(lines):\n    return lines[1:]\n", encoding="utf-8")
        subprocess.run(["git", "add", "tools/pack_pick.py"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "drop first pick line"], cwd=root, check=True, capture_output=True)
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertIn("covers.files changed since", lint.stdout)

    def test_covers_skips_when_git_missing(self):
        repo = copy_tree(SAMPLE)
        rec_path = repo / ".make-it-lint/ledger/F-20260814-01/record.yaml"
        rec = load_record_file(rec_path)
        rec["covers"] = {"files": ["pack_pick.py"], "synced_commit": "deadbeef"}
        save_record_file(rec_path, rec)
        empty = repo / "empty-bin"
        empty.mkdir()
        git = empty / "git"
        git.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        git.chmod(0o755)
        proc = run_mil(
            "lint",
            "--root",
            str(repo),
            env={"PATH": str(empty) + os.pathsep + os.environ.get("PATH", "")},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("git not available; skipped covers.files drift check", proc.stdout)
