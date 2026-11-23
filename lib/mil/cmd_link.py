"""Attach an existing check file to a ledger record."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from mil.checks import CheckError, ensure_check_executable, extract_metadata, resolve_check_program
from mil.errors import MilError, mil_line, rel_to
from mil.ledger import find_by_id, save_record_file
from mil.models import CHECK_TOOLS, LAYERS, RECORD_ID_RE
from mil.paths import mil_dir

LINK_EPILOG = """\
Attach an existing check file to a ledger record. mil link does not edit
the check body: the leading metadata must already have incident: <id>.

record.check gets id, tool, path, and message. tool defaults to the check
metadata's tool (or script). message defaults to the check's first
docstring line; pass --message to override it.

Order that keeps mil lint green: mil record (with --bad and --good),
mil classify (if not classified on creation), write the check, mil link,
then mil prove.
"""

_DOCSTRING_RE = re.compile(r'^\s*(?P<q>"""|\'\'\')(?P<body>.*)$')


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "link",
        help="attach an existing check file to a ledger record",
        description="Attach an existing check file to a ledger record.",
        epilog=LINK_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("incident_id", metavar="F-YYYYMMDD-NN")
    p.add_argument("check", metavar="CHECK", help="check file or directory under .make-it-lint/checks/")
    p.add_argument("--message", help="record.check.message (default: first docstring line)")
    p.add_argument("--tool", choices=list(CHECK_TOOLS), help="check tool (default: check metadata tool or script)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    if not mil_dir(root).is_dir():
        raise MilError(mil_line(root, "no .make-it-lint directory"))
    incident_id = args.incident_id
    if not RECORD_ID_RE.match(incident_id):
        raise MilError(mil_line("link", f"{incident_id!r} is not a record ID (F-YYYYMMDD-NN)"))
    found = find_by_id(root, incident_id)
    if not found:
        raise MilError(mil_line("link", f"{incident_id}: not found"))
    rec_path, rec = found
    state = (rec.get("enforcement") or {}).get("state")
    if state == "human_review":
        raise MilError(mil_line("link", f"{incident_id}: human_review has no check; run mil classify first"))
    if state == "retired":
        raise MilError(mil_line("link", f"{incident_id}: retired record cannot take a new check"))

    check_arg = Path(args.check)
    if not check_arg.is_absolute():
        check_arg = (root / check_arg).resolve() if (root / check_arg).exists() else check_arg.resolve()
    else:
        check_arg = check_arg.resolve()
    try:
        program = resolve_check_program(check_arg)
        metadata = extract_metadata(program)
    except CheckError as exc:
        raise MilError(mil_line("link", str(exc))) from exc

    check_id = metadata.get("id")
    if not isinstance(check_id, str) or not check_id.strip():
        raise MilError(mil_line("link", f"{program}: metadata has no id"))
    incident = metadata.get("incident")
    if not incident:
        raise MilError(
            mil_line(
                "link",
                f"{rel_to(root, program)} has no incident: add 'incident: {incident_id}' to the leading metadata",
            )
        )
    if incident != incident_id:
        raise MilError(
            mil_line("link", f"{rel_to(root, program)} incident {incident} does not match {incident_id}")
        )

    existing = rec.get("check") if isinstance(rec.get("check"), dict) else None
    if existing and existing.get("id") and existing.get("id") != check_id:
        raise MilError(
            mil_line(
                "link",
                f"{incident_id} already has check.id {existing.get('id')}; "
                "linking a different check is a human edit of the record",
            )
        )
    if existing and existing.get("id") == check_id:
        win_note = ensure_check_executable(root, program)
        stored_path = existing.get("path")
        print(
            f"linked {incident_id} {check_id} -> {stored_path} "
            f"state={state} (already attached; record.check kept as stored)"
        )
        wanted = {
            "path": _stored_check_path(root, check_arg, program),
            "tool": args.tool,
            "message": args.message,
        }
        for key, value in wanted.items():
            if value and existing.get(key) != value:
                print(f"note: record.check.{key} is {existing.get(key)!r}; not rewritten (you gave {value!r})")
        if win_note:
            print(win_note)
        return 0

    cls = rec.get("classification")
    layer = rec.get("layer")
    if not isinstance(cls, dict) or layer not in LAYERS:
        raise MilError(
            mil_line(
                "link",
                f"{incident_id}: classify the record with a stop layer before linking a check "
                f"(run mil classify {incident_id} --kind ... --decidable yes --layer ... --layer-reason ...)",
            )
        )
    meta_layer = metadata.get("layer")
    if meta_layer and meta_layer != layer:
        raise MilError(
            mil_line(
                "link",
                f"{rel_to(root, program)} metadata layer {meta_layer!r} does not match record layer {layer!r}",
            )
        )

    meta_tool = metadata.get("tool")
    if args.tool and meta_tool and args.tool != meta_tool:
        raise MilError(
            mil_line(
                "link",
                f"--tool {args.tool!r} does not match check metadata tool {meta_tool!r}",
            )
        )
    tool = args.tool or meta_tool or "script"

    message = args.message or _message_from_check(program)
    if not message and existing:
        existing_msg = existing.get("message")
        if isinstance(existing_msg, str) and existing_msg.strip():
            message = existing_msg
    if not message:
        raise MilError(mil_line("link", "no message: pass --message or put a docstring in the check"))

    stored_path = _stored_check_path(root, check_arg, program)
    rec["check"] = {
        "id": check_id,
        "tool": tool,
        "path": stored_path,
        "message": message,
    }
    rec.setdefault("proof", {})
    if rec["proof"] is None:
        rec["proof"] = {}
    rec["proof"].setdefault("bad", "bad/")
    rec["proof"].setdefault("good", "good/")
    rec["proof"].setdefault("last_result", None)

    win_note = ensure_check_executable(root, program)
    save_record_file(rec_path, rec)
    print(f"linked {incident_id} {check_id} -> {stored_path} state={state}")
    if win_note:
        print(win_note)
    return 0


def _stored_check_path(root: Path, given: Path, program: Path) -> str:
    stored = given
    if program.name.startswith("check.") or program.name == "check":
        stored = program.parent
    elif given.is_file():
        stored = program
    try:
        return stored.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(stored)


def _message_from_check(program: Path) -> str | None:
    try:
        text = program.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    fences = 0
    while i < len(lines) and fences < 2:
        body = lines[i][1:].strip() if lines[i].startswith("#") else None
        if body == "---":
            fences += 1
        i += 1
        if body is None and fences == 0:
            break
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return None
    match = _DOCSTRING_RE.match(lines[i])
    if not match:
        return None
    quote = match.group("q")
    first = match.group("body").strip()
    if first.endswith(quote):
        first = first[: -len(quote)].strip()
    if first:
        return first
    i += 1
    while i < len(lines):
        line = lines[i]
        if quote in line:
            before = line.split(quote, 1)[0].strip()
            return before or None
        if line.strip():
            return line.strip()
        i += 1
    return None
