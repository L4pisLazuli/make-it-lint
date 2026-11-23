"""Pack a pick list. Reorder is allowed; membership of (sku, qty) must stay."""


def pack(lines):
    out = list(lines)
    out.reverse()
    return out
