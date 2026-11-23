#!/usr/bin/env python3
# ---
# id: MIL-DRAFT-001
# origin: sample-harness/draft-in-published
# input: tree
# enforcement:
#   state: shadow
# ---
"""Fail when a published note still has status: draft in its front matter.

Bundled generic checks use origin until an adopter files a ledger record
and adds incident.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def _load_section(config_path: str, check_id: str) -> dict:
    raw = os.environ.get("MIL_CHECK_CONFIG_JSON")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    from mil import yamlio

    cfg = yamlio.load_path(config_path)
    return ((cfg or {}).get("checks") or {}).get(check_id) or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    section = _load_section(args.config, "MIL-DRAFT-001")
    prefixes = section.get("published_paths") or ["published/"]
    message = "published note still has status: draft; set status: public before merge"
    hits = 0
    root = Path(args.root)
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if not _under(rel, prefixes):
            continue
        text = path.read_text(encoding="utf-8")
        if _draft_status(text):
            print(f"{rel}: {message}")
            hits += 1
    return 1 if hits else 0


def _under(rel: str, prefixes: list) -> bool:
    for raw in prefixes:
        prefix = str(raw).rstrip("/")
        if rel == prefix or rel.startswith(prefix + "/"):
            return True
    return False


def _draft_status(text: str) -> bool:
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    fm = text[3:end]
    return re.search(r"(?m)^status:\s*draft\s*$", fm) is not None


if __name__ == "__main__":
    raise SystemExit(main())
