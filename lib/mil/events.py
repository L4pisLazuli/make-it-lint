"""Append-only events.ndjson and a per-check summary."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mil.errors import MilError, mil_line
from mil.paths import events_path, ledger_dir

EVENT_TYPES = (
    "shadow_run",
    "shadow_hit",
    "comment_hit",
    "block_hit",
    "waiver",
    "check_error",
    "prove",
)


def utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_not_under_ledger(path: Path, root: Path) -> None:
    ledger = ledger_dir(root).resolve()
    try:
        path.resolve().relative_to(ledger)
    except ValueError:
        return
    raise MilError(mil_line("events", f"{path} is under ledger/; events stay outside the ledger"))


def append_event(path: Path, event: dict[str, Any], *, root: Path | None = None) -> None:
    if root is not None:
        assert_not_under_ledger(path, root)
    payload = dict(event)
    payload.setdefault("ts", utc_now_z())
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_events(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        combined: list[dict[str, Any]] = []
        for child in sorted(path.rglob("*.ndjson")):
            if child.is_file():
                combined.extend(_load_event_file(child))
        return combined
    return _load_event_file(path)


def _load_event_file(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def summarize(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-check shadow runs, hits, waivers, check errors, and proves."""
    by_check: dict[str, dict[str, int]] = defaultdict(
        lambda: {"shadow_runs": 0, "hits": 0, "waivers": 0, "errors": 0, "proves": 0}
    )
    for event in events:
        check = str(event.get("check") or "-")
        kind = event.get("type")
        detail = str(event.get("detail") or "")
        path = str(event.get("path") or "")
        row = by_check[check]
        if kind == "shadow_run" or (kind == "shadow_hit" and (detail == "ran" or not path)):
            row["shadow_runs"] += 1
        if kind in {"shadow_hit", "comment_hit", "block_hit"} and path:
            row["hits"] += 1
        if kind == "waiver":
            row["waivers"] += 1
        if kind == "check_error":
            row["errors"] += 1
        if kind == "prove":
            row["proves"] += 1
    rows = []
    for check in sorted(by_check):
        rows.append({"check": check, **by_check[check]})
    return rows


def format_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "mil events: no events"
    lines = ["check  shadow_runs  hits  waivers  errors  proves"]
    for row in rows:
        lines.append(
            "%s  %d  %d  %d  %d  %d"
            % (
                row["check"],
                row["shadow_runs"],
                row["hits"],
                row["waivers"],
                row["errors"],
                row["proves"],
            )
        )
    return "\n".join(lines)


def event_sinks(root: Path, extra: str | Path | None, config: dict[str, Any] | None) -> list[Path]:
    sinks: list[Path] = []
    if extra:
        sinks.append(Path(extra))
    if isinstance(config, dict) and (config.get("events") or {}).get("commit"):
        sinks.append(events_path(root))
    unique: list[Path] = []
    seen = set()
    for path in sinks:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
        assert_not_under_ledger(path, root)
    return unique
