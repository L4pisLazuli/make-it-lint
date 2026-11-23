#!/usr/bin/env python3
# ---
# id: MIL-PACK-001
# incident: F-20260814-01
# input: tree
# layer: verify
# tool: property
# ---
"""Fail when pack() drops pick-list lines.

Uses random.Random(MIL_SEED) so two runs with the same seed match.
"""

from __future__ import annotations

import os
import random
import sys
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _support import check_config, emit, is_ledger_fixture, parse_tree_args  # noqa: E402

CHECK_ID = "MIL-PACK-001"
SKUS = ("WIDGET-04", "WIDGET-09", "BOLT-12", "GASKET-03", "CLAMP-07")


def _seed() -> int:
    raw = os.environ.get("MIL_SEED", "0")
    try:
        return int(raw)
    except ValueError:
        return 0


def _pick_lists(rng: random.Random, trials: int) -> list[list[dict[str, int | str]]]:
    lists: list[list[dict[str, int | str]]] = []
    for _ in range(trials):
        n = rng.randint(2, 6)
        lines = []
        for _line in range(n):
            lines.append({"sku": rng.choice(SKUS), "qty": rng.randint(1, 24)})
        lists.append(lines)
    return lists


def _membership(lines: list[dict[str, int | str]]) -> Counter:
    return Counter((str(row["sku"]), int(row["qty"])) for row in lines)


def _load_pack(path: Path):
    ns: dict[str, object] = {}
    exec(path.read_text(encoding="utf-8"), ns, ns)
    pack = ns.get("pack")
    if not callable(pack):
        raise SystemExit(2)
    return pack


def main(argv: list[str] | None = None) -> int:
    args = parse_tree_args(argv)
    root = Path(args.root)
    cfg = check_config(Path(args.config), CHECK_ID)
    module = str(cfg.get("module") or "pack_pick.py")
    trials = int(cfg.get("trials") or 20)
    message = cfg.get("message") or (
        "pack() must keep every pick line (sku, qty) for random lists drawn from MIL_SEED"
    )
    rng = random.Random(_seed())
    cases = _pick_lists(rng, trials)
    n_hits = 0
    for path in sorted(root.rglob(module)):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if is_ledger_fixture(rel):
            continue
        pack = _load_pack(path)
        for lines in cases:
            packed = pack(list(lines))
            if _membership(list(packed)) != _membership(lines):
                emit(rel, message)
                n_hits += 1
                break
    return 1 if n_hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
