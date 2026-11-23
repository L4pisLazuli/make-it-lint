"""Guard protected paths and evaluate PreToolUse actions (`mil guard`)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from mil import yamlio
from mil.errors import MilError, mil_line
from mil.events import event_sinks
from mil.git_tree import has_head, materialize_ref, materialize_staged
from mil.guard import audit_diff, evaluate_action, normalize_hook_payload
from mil.paths import config_path, mil_dir

GUARD_EPILOG = """\
Two modes:

1. Pre-tool / action guard (used by Claude Code & Cursor PreToolUse hooks):
     mil guard --hook
     mil guard --action action.json

   Blocks direct agent file edits to .make-it-lint/waivers.yaml, existing
   checks, existing record.yaml files, installed kit files, and configured
   protected_paths, and runs active (shadow/comment/block) `input: action`
   checks against the normalized tool call.

2. Diff governance audit (used in pre-commit and CI):
     mil guard --staged
     mil guard --base-ref origin/main --head-ref HEAD
     mil guard --base /tmp/base --head /tmp/head

   Audits changes to protected/governance paths and fails (exit 1) if an
   existing ledger record's history or identity was tampered with.
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "guard",
        help="guard protected paths and run input: action checks",
        description="Guard protected paths and run input: action checks.",
        epilog=GUARD_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("--hook", action="store_true", help="read Claude Code / Cursor PreToolUse JSON on stdin")
    p.add_argument("--action", metavar="FILE", help="evaluate an action.json file")
    p.add_argument("--staged", action="store_true", help="audit HEAD vs staged git index")
    p.add_argument("--base", metavar="DIR", help="base directory for diff audit")
    p.add_argument("--head", metavar="DIR", help="head directory for diff audit")
    p.add_argument("--base-ref", metavar="REF", help="git ref for base tree in diff audit")
    p.add_argument("--head-ref", metavar="REF", help="git ref for head tree in diff audit (default: HEAD)")
    p.add_argument("--events", metavar="FILE", help="append hit/waiver events as JSON lines")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    if not mil_dir(root).is_dir():
        if args.hook:
            return 0
        raise MilError(mil_line(root, "no .make-it-lint directory"))

    cfg_data = _load_config(config_path(root))
    sinks = event_sinks(root, getattr(args, "events", None), cfg_data)

    if args.hook:
        return _run_hook(root, cfg_data, sinks)
    if args.action:
        return _run_action_file(root, Path(args.action), cfg_data, sinks)
    if args.staged or args.base or args.base_ref:
        return _run_diff_audit(root, args, cfg_data)

    raise MilError(
        mil_line("guard", "specify --hook, --action FILE, --staged, --base/--head, or --base-ref")
    )


def _read_stdin_utf8() -> str:
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return buf.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def _run_hook(root: Path, cfg_data: dict[str, Any] | None, sinks: list[Path]) -> int:
    try:
        payload = json.loads(_read_stdin_utf8())
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    event_name = str(payload.get("hook_event_name") or "PreToolUse")
    action = normalize_hook_payload(payload, root)
    verdict = evaluate_action(root, action, config_data=cfg_data, sinks=sinks)

    if verdict["decision"] == "deny":
        reasons = []
        for hit in verdict["blocks"]:
            loc = f"{hit['path']}: " if hit.get("path") else ""
            reasons.append(f"[{hit['check']}] {loc}{hit['message']}")
        for err in verdict["errors"]:
            reasons.append(f"[{err['check']}] check error: {err['detail']}")
        reason_text = "make-it-lint blocked this action: " + "; ".join(reasons)
        if event_name == "preToolUse":
            print(
                json.dumps(
                    {
                        "permission": "deny",
                        "user_message": reason_text,
                        "agent_message": reason_text,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason_text,
                        }
                    },
                    ensure_ascii=False,
                )
            )
        return 0

    if verdict["comments"]:
        notes = []
        for hit in verdict["comments"]:
            loc = f"{hit['path']}: " if hit.get("path") else ""
            notes.append(f"[{hit['check']}] {loc}{hit['message']}")
        note_text = "make-it-lint warning: " + "; ".join(notes)
        if event_name == "preToolUse":
            print(json.dumps({"permission": "allow", "agent_message": note_text}, ensure_ascii=False))
        else:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "additionalContext": note_text,
                        }
                    },
                    ensure_ascii=False,
                )
            )
    return 0


def _run_action_file(
    root: Path, action_path: Path, cfg_data: dict[str, Any] | None, sinks: list[Path]
) -> int:
    if not action_path.is_file():
        raise MilError(mil_line("guard", f"--action {action_path}: file not found"))
    try:
        raw = json.loads(action_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise MilError(mil_line("guard", f"--action {action_path}: invalid JSON: {exc}")) from exc
    if not isinstance(raw, dict):
        raise MilError(mil_line("guard", f"--action {action_path}: must be a JSON object"))
    action = raw if ("kind" in raw and "tool" in raw) else normalize_hook_payload(raw, root)
    verdict = evaluate_action(root, action, config_data=cfg_data, sinks=sinks)
    for hit in verdict["shadows"] + verdict["comments"] + verdict["blocks"]:
        loc = hit["path"] + (f":{hit['line']}" if hit.get("line") else "") if hit.get("path") else "-"
        print(f"{hit['state']:<7} {hit['check']} {loc}: {hit['message']}")
    for err in verdict["errors"]:
        print(f"ERR     {err['check']}: {err['detail']}")
    if verdict["errors"]:
        return 2
    return 1 if verdict["blocks"] else 0


def _run_diff_audit(root: Path, args: argparse.Namespace, cfg_data: dict[str, Any] | None) -> int:
    with tempfile.TemporaryDirectory(prefix="mil-guard-") as tmp:
        tmp_dir = Path(tmp)
        if args.staged:
            base_dir = tmp_dir / "base"
            head_dir = tmp_dir / "head"
            base_dir.mkdir()
            if has_head(root):
                materialize_ref(root, "HEAD", base_dir)
            materialize_staged(root, head_dir)
        elif args.base_ref:
            base_dir = tmp_dir / "base"
            head_dir = tmp_dir / "head"
            materialize_ref(root, args.base_ref, base_dir)
            materialize_ref(root, args.head_ref or "HEAD", head_dir)
        else:
            if not args.base:
                raise MilError(mil_line("guard", "--base DIR is required when not using --staged or --base-ref"))
            base_dir = Path(args.base).resolve()
            head_dir = Path(args.head).resolve() if args.head else root
            if not base_dir.is_dir():
                raise MilError(mil_line("guard", f"--base {args.base}: not a directory"))
            if not head_dir.is_dir():
                raise MilError(mil_line("guard", f"--head {head_dir}: not a directory"))

        findings = audit_diff(base_dir, head_dir, root=root, config_data=cfg_data)

    blocked = 0
    for item in findings:
        tag = "BLOCK" if item["severity"] == "block" else "REVIEW"
        print(f"{tag:<6} {item['kind']} {item['path']}: {item['detail']}")
        if item["severity"] == "block":
            blocked += 1
    print(f"mil guard: {len(findings)} finding(s), {blocked} blocked")
    return 1 if blocked else 0


def _load_config(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = yamlio.load_path(str(path))
    except yamlio.YAMLError:
        return None
    return data if isinstance(data, dict) else None
