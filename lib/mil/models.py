"""Ledger constants and allowed state transitions.

Lifecycle:
  open -> proven -> shadow -> comment -> block
  open <-> human_review
  block -> comment -> shadow -> proven (via mil demote)
  any non-retired state -> retired (via mil retire)

LAYERS is strongest to weakest. A recurring failure should move toward
architecture, not toward weaker layers.
"""

from __future__ import annotations

import re

STATES = (
    "open",
    "proven",
    "shadow",
    "comment",
    "block",
    "human_review",
    "retired",
)

ROLLOUT = (
    "proven",
    "shadow",
    "comment",
    "block",
)

LAYERS = (
    "architecture",
    "type",
    "lint",
    "test",
    "verify",
    "hook",
    "ci",
)

CHECK_INPUTS = (
    "tree",
    "diff",
    "action",
)

CHECK_TOOLS = (
    "script",
    "semgrep",
    "ast-grep",
    "test",
    "hook",
    "property",
    "runtime",
    "model",
)

EVIDENCE_KINDS = (
    "video",
    "screenshot",
    "trace",
    "diagram",
    "counterexample",
    "log",
    "transcript",
)

KIND_DEFAULTS = {
    "architecture": {"layer": "architecture", "tool": "script"},
    "type": {"layer": "type", "tool": "script"},
    "lint": {"layer": "lint", "tool": "script"},
    "boot": {"layer": "hook", "tool": "hook"},
    "scope": {"layer": "ci", "tool": "script"},
    "data": {"layer": "verify", "tool": "property"},
    "freeze": {"layer": "verify", "tool": "model"},
    "reporting": {"layer": "test", "tool": "test"},
    "judgment": {"layer": None, "tool": None, "state": "human_review"},
    "dependency": {"layer": "ci", "tool": "script"},
    "test": {"layer": "test", "tool": "test"},
}

DECIDABLE = ("yes", "partial", "no")

PROVEN_OR_LATER = {"proven", "shadow", "comment", "block"}
RUNS_ON_PRODUCT = {"shadow", "comment", "block"}

PROVEN_RESULT = "red-on-bad, green-on-good"
PROVEN_MULTI_RE = re.compile(
    r"^red-on-bad (\d+)/(\d+), green-on-good (\d+)/(\d+)$"
)

HIT_PARSE_RE = re.compile(r"^((?:[A-Za-z]:[/\\])?[^\s:][^:]*?)(?::(\d+))?: (.+)$")

FINGERPRINT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$")
RECORD_ID_RE = re.compile(r"^F-([0-9]{8})-([0-9]{2,})$")


def is_proven_result(value: str | None) -> bool:
    """True for the single-pair form or red-on-bad N/N, green-on-good M/M."""
    if value == PROVEN_RESULT:
        return True
    if not isinstance(value, str):
        return False
    match = PROVEN_MULTI_RE.fullmatch(value)
    if not match:
        return False
    red_bad, n_bad, green_good, n_good = (int(part) for part in match.groups())
    return red_bad == n_bad > 0 and green_good == n_good > 0


def proven_result_text(*, red_bad: int, n_bad: int, green_good: int, n_good: int, multi: bool) -> str:
    if not multi:
        r_bad = "red-on-bad" if red_bad == 1 and n_bad == 1 else (
            "green-on-bad" if red_bad == 0 and n_bad == 1 else "error-on-bad"
        )
        r_good = "green-on-good" if green_good == 1 and n_good == 1 else (
            "red-on-good" if green_good == 0 and n_good == 1 else "error-on-good"
        )
        return "%s, %s" % (r_bad, r_good)
    return "red-on-bad %d/%d, green-on-good %d/%d" % (red_bad, n_bad, green_good, n_good)


def parse_hit_line(line: str) -> tuple[str, int | None, str] | None:
    """Split a contract hit line into (path, line or None, message)."""
    match = HIT_PARSE_RE.match(line)
    if not match:
        return None
    path, lineno, message = match.group(1), match.group(2), match.group(3)
    return path, int(lineno) if lineno else None, message


class _HitPattern:
    """Compatibility helper matching valid hit lines via parse_hit_line."""

    @staticmethod
    def match(line: str) -> bool:
        return parse_hit_line(line) is not None


HIT_RE = _HitPattern()


def allowed_next(current: str) -> set[str]:
    if current == "open":
        return {"proven", "human_review", "retired"}
    if current == "human_review":
        return {"open", "retired"}
    if current == "proven":
        return {"shadow", "open", "retired"}
    if current == "shadow":
        return {"comment", "proven", "retired"}
    if current == "comment":
        return {"block", "shadow", "retired"}
    if current == "block":
        return {"comment", "retired"}
    if current == "retired":
        return {"open"}
    return set()


def transitions_ok(history_states: list[str]) -> str | None:
    """Return an error message if history skips rollout steps or jumps illegally."""
    if not history_states:
        return "history is empty"
    if history_states[0] not in {"open", "human_review"}:
        return f"history must start at open or human_review, not {history_states[0]!r}"
    for prev, cur in zip(history_states, history_states[1:]):
        if cur == prev:
            continue
        if cur not in allowed_next(prev):
            return f"illegal transition {prev!r} -> {cur!r}"
    return None


def stronger_layer(layer: str | None) -> str | None:
    """One step toward architecture. None if already strongest or unknown."""
    if layer not in LAYERS:
        return None
    i = LAYERS.index(layer)
    if i == 0:
        return None
    return LAYERS[i - 1]


def next_layer(layer: str | None) -> str | None:
    return stronger_layer(layer)


INCIDENT_KEY_ORDER = [
    "id",
    "title",
    "origin",
    "what_happened",
    "human_words",
    "occurrences",
    "fingerprint",
    "classification",
    "layer",
    "layer_reason",
    "check",
    "proof",
    "evidence",
    "covers",
    "enforcement",
    "owner",
    "history",
]

ORIGIN_KEY_ORDER = ["inventory_id", "source", "actor", "reporter"]
CLASSIFICATION_KEY_ORDER = ["kind", "decidable"]
CHECK_KEY_ORDER = ["id", "tool", "path", "message"]
PROOF_KEY_ORDER = ["bad", "good", "last_result", "seed"]
EVIDENCE_ITEM_KEY_ORDER = ["kind", "path", "url", "sha256", "note"]
COVERS_KEY_ORDER = ["files", "synced_commit"]
ENFORCEMENT_KEY_ORDER = ["state", "promote_when"]
HISTORY_KEY_ORDER = ["state", "at", "from_state", "approved_by", "by", "note"]
OCCURRENCE_KEY_ORDER = ["at", "ref", "note"]


def ordered_incident(record: dict) -> dict:
    """Return a copy with stable key order so dumps stay readable."""
    out = _take(record, INCIDENT_KEY_ORDER)
    if isinstance(out.get("origin"), dict):
        out["origin"] = _take(out["origin"], ORIGIN_KEY_ORDER)
    if isinstance(out.get("classification"), dict):
        out["classification"] = _take(out["classification"], CLASSIFICATION_KEY_ORDER)
    if isinstance(out.get("check"), dict):
        out["check"] = _take(out["check"], CHECK_KEY_ORDER)
    if isinstance(out.get("proof"), dict):
        out["proof"] = _take(out["proof"], PROOF_KEY_ORDER)
    if isinstance(out.get("evidence"), list):
        out["evidence"] = [
            _take(item, EVIDENCE_ITEM_KEY_ORDER) if isinstance(item, dict) else item
            for item in out["evidence"]
        ]
    if isinstance(out.get("covers"), dict):
        out["covers"] = _take(out["covers"], COVERS_KEY_ORDER)
    if isinstance(out.get("enforcement"), dict):
        out["enforcement"] = _take(out["enforcement"], ENFORCEMENT_KEY_ORDER)
    if isinstance(out.get("history"), list):
        out["history"] = [
            _take(item, HISTORY_KEY_ORDER) if isinstance(item, dict) else item for item in out["history"]
        ]
    if isinstance(out.get("occurrences"), list):
        out["occurrences"] = [
            _take(item, OCCURRENCE_KEY_ORDER) if isinstance(item, dict) else item
            for item in out["occurrences"]
        ]
    return out


def _take(data: dict, order: list[str]) -> dict:
    out = {}
    for key in order:
        if key in data:
            out[key] = data[key]
    for key, value in data.items():
        if key not in out:
            out[key] = value
    return out
