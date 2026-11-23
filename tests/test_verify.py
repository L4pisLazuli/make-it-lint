from __future__ import annotations

import os
import unittest

from tests.support import SAMPLE, run_mil, scratch_adopter

from mil.ledger import load_record_file


NOISY = '''#!/usr/bin/env python3
# ---
# id: MIL-NOISE-001
# incident: {incident}
# input: tree
# layer: verify
# tool: property
# ---
"""Prints a fresh random integer every run; ignores MIL_SEED."""
import argparse
import random
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--root")
parser.add_argument("--config")
args = parser.parse_args()
print("pack_pick.py:%d: qty total drifted" % random.randint(10**6, 10**7 - 1))
raise SystemExit(1)
'''

SEEDED = '''#!/usr/bin/env python3
# ---
# id: MIL-SEED-001
# incident: {incident}
# input: tree
# layer: verify
# tool: property
# ---
"""Uses MIL_SEED; two runs match."""
import argparse
import os
import random
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--root")
parser.add_argument("--config")
args = parser.parse_args()
rng = random.Random(int(os.environ.get("MIL_SEED", "0")))
root = Path(args.root)
target = root / "pack_pick.py"
if target.is_file() and "DROP_LAST" in target.read_text(encoding="utf-8"):
    print("pack_pick.py:%d: pack dropped a pick line" % (10 + rng.randint(0, 4)))
    raise SystemExit(1)
raise SystemExit(0)
'''


def _record(root, *, layer="verify"):
    bad = root / "failing"
    good = root / "fixed"
    bad.mkdir()
    good.mkdir()
    (bad / "pack_pick.py").write_text("DROP_LAST = True\n", encoding="utf-8")
    (good / "pack_pick.py").write_text("KEEP = True\n", encoding="utf-8")
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
        "pack() dropped the last SKU when the list had more than one line.",
        "--kind",
        "data",
        "--decidable",
        "yes",
        "--layer",
        layer,
        "--layer-reason",
        "Membership of (sku, qty) is an invariant a seeded property check can draw.",
        "--bad",
        str(bad),
        "--good",
        str(good),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return load_record_file(next((root / ".make-it-lint/ledger").glob("F-*/record.yaml")))


class VerifyLayerTests(unittest.TestCase):
    def test_sample_pack_check_is_reproducible(self):
        proc = run_mil("prove", "F-20260814-01", "--dry-run", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("not reproducible", proc.stdout)

    def test_ignoring_seed_is_not_reproducible(self):
        root = scratch_adopter()
        rec = _record(root)
        check = root / ".make-it-lint/checks/noisy.py"
        check.write_text(NOISY.format(incident=rec["id"]), encoding="utf-8")
        os.chmod(check, 0o755)
        link = run_mil("link", rec["id"], ".make-it-lint/checks/noisy.py", "--root", str(root), "--tool", "property")
        self.assertEqual(link.returncode, 0, link.stdout + link.stderr)
        proc = run_mil("prove", rec["id"], "--dry-run", "--root", str(root))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("not reproducible", proc.stdout + proc.stderr)

    def test_seeded_verify_check_proves(self):
        root = scratch_adopter()
        rec = _record(root)
        check = root / ".make-it-lint/checks/seeded.py"
        check.write_text(SEEDED.format(incident=rec["id"]), encoding="utf-8")
        os.chmod(check, 0o755)
        link = run_mil("link", rec["id"], ".make-it-lint/checks/seeded.py", "--root", str(root), "--tool", "property")
        self.assertEqual(link.returncode, 0, link.stdout + link.stderr)
        proc = run_mil("prove", rec["id"], "--root", str(root))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("red-on-bad, green-on-good", proc.stdout)
        self.assertNotIn("not reproducible", proc.stdout)

    def test_mil_seed_is_set_from_proof_seed(self):
        import tempfile
        from pathlib import Path as P
        from mil.checks import invoke_check

        tmp = P(tempfile.mkdtemp(prefix="mil-seed-"))
        fixture = tmp / "bad"
        fixture.mkdir()
        (fixture / "pack_pick.py").write_text("def pack(lines):\n    return list(lines)\n", encoding="utf-8")
        check = tmp / "show_seed.py"
        check.write_text("import os\nprint('seed=' + os.environ.get('MIL_SEED', '<unset>'))\n", encoding="utf-8")
        proc = invoke_check(
            check, {"input": "tree"}, root=tmp, config=tmp / "config.yaml", fixture_dir=fixture, seed=17
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("seed=17", proc.stdout)
