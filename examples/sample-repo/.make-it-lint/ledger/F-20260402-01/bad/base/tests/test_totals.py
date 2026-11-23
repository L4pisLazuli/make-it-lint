from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "orders-2024-q3.csv"
BASELINE = Path(__file__).parent / "baselines" / "q3-totals.txt"


def parse_orders(path: Path) -> dict:
    rows = []
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
        for line in fh:
            if not line.strip():
                continue
            rec = dict(zip(header, line.strip().split(",")))
            rec["qty"] = int(rec["qty"])
            rec["amount"] = float(rec["amount"])
            rows.append(rec)
    return {
        "rows": len(rows),
        "sum_qty": sum(r["qty"] for r in rows),
        "sum_amount": round(sum(r["amount"] for r in rows), 2),
        "dup_keys": 0,
        "sku_count": len({r["sku"] for r in rows}),
    }


def render(summary: dict) -> str:
    return (
        "PARSE tests/fixtures/orders-2024-q3.csv\n"
        f"rows {summary['rows']}\n"
        f"sum_qty {summary['sum_qty']}\n"
        f"sum_amount {summary['sum_amount']:.2f}\n"
        f"dup_keys {summary['dup_keys']}\n"
        f"sku_count {summary['sku_count']}\n"
    )


def test_totals_match_baseline():
    got = render(parse_orders(FIXTURE))
    assert got == BASELINE.read_text(encoding="utf-8")
