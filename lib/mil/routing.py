"""Suggested layer and tool for a classification kind. Suggestion only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mil import yamlio
from mil.models import KIND_DEFAULTS
from mil.paths import config_path


def load_kind_defaults(root: Path) -> dict[str, dict[str, Any]]:
    merged = {key: dict(value) for key, value in KIND_DEFAULTS.items()}
    path = config_path(root)
    if not path.is_file():
        return merged
    try:
        data = yamlio.load_path(str(path))
    except yamlio.YAMLError:
        return merged
    if not isinstance(data, dict):
        return merged
    extra = (data.get("ledger") or {}).get("kind_defaults") if isinstance(data.get("ledger"), dict) else None
    if not isinstance(extra, dict):
        return merged
    for kind, spec in extra.items():
        if not isinstance(spec, dict):
            continue
        merged[str(kind)] = dict(spec)
    return merged


def suggest_for_kind(root: Path, kind: str | None) -> str | None:
    if not kind:
        return None
    spec = load_kind_defaults(root).get(kind)
    if not spec:
        return "suggest: kind=%s -> (no default; see the routing table in the skill reference)" % kind
    state = spec.get("state")
    layer = spec.get("layer")
    tool = spec.get("tool")
    if state == "human_review" or (layer is None and tool is None and state):
        return "suggest: kind=%s -> %s (suggestion only; mil does not set this)" % (kind, state)
    parts = []
    if layer:
        parts.append("layer=%s" % layer)
    if tool:
        parts.append("tool=%s" % tool)
    if not parts:
        return "suggest: kind=%s -> (no default; see the routing table in the skill reference)" % kind
    return "suggest: kind=%s -> %s (suggestion only; mil does not set this)" % (kind, " ".join(parts))
