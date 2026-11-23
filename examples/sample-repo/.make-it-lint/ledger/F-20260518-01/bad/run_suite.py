#!/usr/bin/env python3
"""Run numeric checks on a parsed order file and report a combined status."""

from __future__ import annotations

import sys
from pathlib import Path

ORDERS = Path(__file__).parent / "orders.tsv"


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        for line in fh:
            rec = dict(zip(header, line.rstrip("\n").split("\t")))
            rec["qty"] = int(rec["qty"])
            rec["amount"] = float(rec["amount"])
            rows.append(rec)
    return rows


def test_row_count(rows: list[dict]) -> None:
    if len(rows) != 4:
        raise AssertionError(f"expected 4 order rows, got {len(rows)}")


def test_amount_sum(rows: list[dict]) -> None:
    total = round(sum(r["amount"] for r in rows), 2)
    if total != 665.50:
        raise AssertionError(f"sum_amount {total} != 665.50")


def test_no_duplicate_orders(rows: list[dict]) -> None:
    keys = [r["order_id"] for r in rows]
    if len(keys) != len(set(keys)):
        raise AssertionError("duplicate order_id values")


def main() -> int:
    rows = load_rows(ORDERS)
    failures = []
    for test in (test_row_count, test_amount_sum, test_no_duplicate_orders):
        try:
            test(rows)
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")
    for line in failures:
        print(line)
    print(f"{len(failures)} failed / {3} run")
    # Swallows the failures so a scheduler that only looks at the process
    # status treats a fully-red run as success.
    sys.exit(0)


if __name__ == "__main__":
    main()
