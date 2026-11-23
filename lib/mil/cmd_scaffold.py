"""Generate a provisional or template check for an open ledger record and link it."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from typing import Any

from mil.checks import discover_checks, ensure_check_executable
from mil.draft import (
    ClassificationPlan,
    detect_toolchain,
    infer_plan,
    render_draft_bundle,
    verify_provisional_check,
)
from mil.errors import MilError, mil_line
from mil.ledger import find_by_id, iter_records, save_record_file
from mil.models import CHECK_INPUTS, CHECK_TOOLS, LAYERS, RECORD_ID_RE
from mil.paths import as_rel, checks_dir, is_within_root, mil_dir

CHECK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

SCAFFOLD_EPILOG = """\
Inspects the record's bad/ and good/ fixtures, selects a matching check input
and tool, synthesizes a provisional check (provisional draft) under .make-it-lint/checks/
whenever a structural AST, token, config, diff-scope, or action rule cleanly
separates bad/ from good/, self-checks it against every fixture variant, sets
executable permissions, and links it to the open ledger record.

If the failure requires behavioral/property logic or a provisional rule cannot
cleanly separate bad/ and good/ (or --no-draft is passed), mil scaffold writes
a ready-to-edit template that exits 2 until edited so an unfinished scaffold
can never pass `mil prove` by mistake.

Defaults when omitted:
  --id      next unused MIL-<KIND>-NNN from the record's fingerprint
  --path    .make-it-lint/checks/<slug>.py (or <slug>/ directory for semgrep / ast-grep)
  --input   inferred from fixtures and stop layer (action / diff / tree)
  --tool    inferred from fixtures, stop layer, and repo toolchain
  --message record.title

usage:
  mil scaffold F-20261123-01
  mil scaffold F-20261123-01 --auto
  mil scaffold F-20261123-01 --tool ast-grep
  mil scaffold F-20261123-01 --no-draft --id MIL-BP-001 --path .make-it-lint/checks/no_breakpoint.py
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "scaffold",
        help="draft or scaffold a check for an open record and link it",
        description="Synthesize a provisional check (or manual template) for an open ledger record and link it.",
        epilog=SCAFFOLD_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("incident_id", metavar="F-YYYYMMDD-NN")
    p.add_argument(
        "--auto",
        action="store_true",
        help="if the record is not yet classified with a stop layer, infer kind/layer/reason from fixtures first",
    )
    p.add_argument(
        "--no-draft",
        action="store_true",
        help="write an unimplemented exit-2 template without synthesizing provisional rule logic",
    )
    p.add_argument("--id", dest="check_id", help="check ID (default: MIL-<KIND>-NNN)")
    p.add_argument("--path", dest="check_path", help="output path under .make-it-lint/checks/")
    p.add_argument("--input", dest="check_input", choices=list(CHECK_INPUTS), help="tree, diff, or action")
    p.add_argument("--tool", dest="check_tool", choices=list(CHECK_TOOLS), help="check tool")
    p.add_argument("--message", help="record.check.message and check docstring (default: record title)")
    p.set_defaults(func=run)


def _default_input_for_layer(layer: str) -> str:
    if layer == "hook":
        return "action"
    if layer == "ci":
        return "diff"
    return "tree"


def _default_tool_for_layer(layer: str) -> str:
    if layer == "hook":
        return "hook"
    if layer == "verify":
        return "property"
    if layer == "test":
        return "test"
    return "script"


def _used_check_ids(root: Path) -> set[str]:
    used: set[str] = set()
    found, _ = discover_checks(root)
    for _, meta in found:
        cid = meta.get("id")
        if isinstance(cid, str) and cid:
            used.add(cid)
    for _, rec in iter_records(root):
        chk = rec.get("check")
        if isinstance(chk, dict) and isinstance(chk.get("id"), str):
            used.add(chk["id"])
    return used


def _next_check_id(root: Path, fp_kind: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "", fp_kind).upper() or "RULE"
    used = _used_check_ids(root)
    idx = 1
    while True:
        candidate = f"MIL-{token}-{idx:03d}"
        if candidate not in used:
            return candidate
        idx += 1


def _default_check_stem(root: Path, fp_slug: str, incident_id: str, *, is_bundle: bool) -> str:
    stem = re.sub(r"[^a-z0-9]+", "_", fp_slug.lower()).strip("_") or "check_rule"
    candidate = f".make-it-lint/checks/{stem}" if is_bundle else f".make-it-lint/checks/{stem}.py"
    if not (root / candidate).exists() and not (root / f".make-it-lint/checks/{stem}").exists():
        return f".make-it-lint/checks/{stem}"
    inc_suffix = incident_id.lower().replace("-", "_")
    return f".make-it-lint/checks/{stem}_{inc_suffix}"


def _has_nonempty_fixtures(bad_dir: Path, good_dir: Path) -> bool:
    return (
        bad_dir.is_dir()
        and good_dir.is_dir()
        and any(c for c in bad_dir.iterdir() if not c.name.startswith("."))
        and any(c for c in good_dir.iterdir() if not c.name.startswith("."))
    )


def _write_bundle_files(root: Path, files: dict[str, str]) -> None:
    for rel_file, content in files.items():
        fpath = root / rel_file
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8", newline="\n")


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    if not mil_dir(root).is_dir():
        raise MilError(mil_line(root, "no .make-it-lint directory"))
    incident_id = args.incident_id
    if not RECORD_ID_RE.match(incident_id):
        raise MilError(mil_line("scaffold", f"{incident_id!r} is not a record ID (F-YYYYMMDD-NN)"))
    found = find_by_id(root, incident_id)
    if not found:
        raise MilError(mil_line("scaffold", f"{incident_id}: not found"))
    rec_path, rec = found

    state = (rec.get("enforcement") or {}).get("state")
    if state == "human_review":
        raise MilError(mil_line("scaffold", f"{incident_id}: human_review has no check; run mil classify first"))
    if state == "retired":
        raise MilError(mil_line("scaffold", f"{incident_id}: retired record cannot take a new check"))
    if state != "open":
        raise MilError(
            mil_line("scaffold", f"{incident_id}: state is {state}; scaffold only creates a check for open records")
        )

    existing_chk = rec.get("check")
    if isinstance(existing_chk, dict) and existing_chk.get("id"):
        raise MilError(
            mil_line(
                "scaffold",
                f"{incident_id} already has check {existing_chk.get('id')} ({existing_chk.get('path')})",
            )
        )

    proof_existing = rec.get("proof") if isinstance(rec.get("proof"), dict) else {}
    bad_dir = rec_path.parent / as_rel(str(proof_existing.get("bad") or "bad/"))
    good_dir = rec_path.parent / as_rel(str(proof_existing.get("good") or "good/"))
    has_fixtures = _has_nonempty_fixtures(bad_dir, good_dir)
    tc = detect_toolchain(root)

    cls = rec.get("classification")
    layer = rec.get("layer")
    if not isinstance(cls, dict) or layer not in LAYERS:
        if not args.auto:
            raise MilError(
                mil_line(
                    "scaffold",
                    f"{incident_id}: classify the record with a stop layer first "
                    f"(run mil classify {incident_id} --auto or pass mil scaffold {incident_id} --auto)",
                )
            )
        if not has_fixtures:
            raise MilError(
                mil_line("scaffold", f"{incident_id}: --auto requires non-empty bad/ and good/ fixtures on the record")
            )
        auto_plan = infer_plan(
            root,
            bad_dir,
            good_dir,
            toolchain=tc,
            explicit_tool=args.check_tool,
            explicit_input=args.check_input,
        )
        rec["classification"] = {"kind": auto_plan.kind, "decidable": auto_plan.decidable}
        rec["layer"] = auto_plan.layer
        rec["layer_reason"] = auto_plan.layer_reason
        layer = auto_plan.layer
        print(
            f"auto-selected: kind={auto_plan.kind} decidable={auto_plan.decidable} "
            f"layer={layer} tool={auto_plan.tool} input={auto_plan.check_input} (strategy={auto_plan.strategy})"
        )

    if has_fixtures:
        plan = infer_plan(
            root,
            bad_dir,
            good_dir,
            toolchain=tc,
            explicit_layer=str(layer),
            explicit_tool=args.check_tool,
            explicit_input=args.check_input,
        )
    else:
        plan = ClassificationPlan(
            kind=str((rec.get("classification") or {}).get("kind") or "lint"),
            decidable="yes",
            layer=str(layer),
            tool=args.check_tool or _default_tool_for_layer(str(layer)),
            check_input=args.check_input or _default_input_for_layer(str(layer)),
            layer_reason=str(rec.get("layer_reason") or ""),
            strategy="manual_template",
        )

    fp = str(rec.get("fingerprint") or f"{plan.kind}:rule")
    fp_kind, _, fp_slug = fp.partition(":")
    if not fp_slug:
        fp_slug = "rule"
    if fp_kind in ("unclassified", "ci") and isinstance(rec.get("classification"), dict):
        fp_kind = str(rec["classification"].get("kind") or fp_kind)

    used_ids = _used_check_ids(root)
    if args.check_id:
        check_id = args.check_id.strip()
        if not CHECK_ID_RE.match(check_id):
            raise MilError(mil_line("scaffold", f"invalid check ID {check_id!r}"))
        if check_id in used_ids:
            raise MilError(mil_line("scaffold", f"check ID {check_id!r} is already in use"))
    else:
        check_id = _next_check_id(root, fp_kind)

    message = (args.message or str(rec.get("title") or "")).strip()
    if not message:
        raise MilError(mil_line("scaffold", "no message: pass --message"))

    is_bundle = plan.tool in ("semgrep", "ast-grep")
    cdir = checks_dir(root)
    cdir.mkdir(parents=True, exist_ok=True)
    if args.check_path:
        raw_target = Path(args.check_path)
        target_path = raw_target.resolve() if raw_target.is_absolute() else (root / raw_target).resolve()
        if not is_within_root(cdir, target_path) or target_path == cdir.resolve():
            raise MilError(mil_line("scaffold", "--path must point inside .make-it-lint/checks/"))
        if not is_bundle and target_path.suffix.lower() != ".py":
            raise MilError(mil_line("scaffold", "--path must end with .py"))
        rel_arg = target_path.relative_to(root.resolve()).as_posix()
        base_rel_stem = rel_arg[:-3] if rel_arg.endswith(".py") else rel_arg
    else:
        base_rel_stem = _default_check_stem(root, fp_slug, incident_id, is_bundle=is_bundle)

    bundle = render_draft_bundle(
        plan=plan,
        check_id=check_id,
        incident_id=incident_id,
        message=message,
        base_rel_stem=base_rel_stem,
        allow_provisional=not args.no_draft,
    )

    stored_target = (root / bundle.stored_rel).resolve()
    if stored_target.exists() or any((root / rel_f).exists() for rel_f in bundle.files):
        raise MilError(
            mil_line(
                "scaffold",
                f"{bundle.stored_rel} already exists; existing check files are protected (choose another --path)",
            )
        )

    _write_bundle_files(root, bundle.files)
    win_note = ensure_check_executable(root, (root / bundle.program_rel).resolve())

    can_dry_run_locally = True
    if plan.tool == "semgrep" and not tc.semgrep_bin:
        can_dry_run_locally = False
    elif plan.tool == "ast-grep" and not tc.ast_grep_bin:
        can_dry_run_locally = False

    dry_summary = ""
    fallback_reason = ""
    if bundle.is_provisional_rule and has_fixtures and can_dry_run_locally:
        ok, dry_summary = verify_provisional_check(root, rec_path.parent, rec, stored_target)
        if not ok:
            fallback_reason = dry_summary
            if stored_target.is_dir():
                shutil.rmtree(stored_target, ignore_errors=True)
            else:
                stored_target.unlink(missing_ok=True)
            bundle = render_draft_bundle(
                plan=plan,
                check_id=check_id,
                incident_id=incident_id,
                message=message,
                base_rel_stem=base_rel_stem,
                allow_provisional=False,
            )
            _write_bundle_files(root, bundle.files)
            win_note = ensure_check_executable(root, (root / bundle.program_rel).resolve())

    rec["check"] = {
        "id": check_id,
        "tool": plan.tool,
        "path": bundle.stored_rel,
        "message": message,
    }
    proof: dict[str, Any] = dict(proof_existing)
    proof.setdefault("bad", "bad/")
    proof.setdefault("good", "good/")
    proof.setdefault("last_result", None)
    rec["proof"] = proof
    save_record_file(rec_path, rec)

    if bundle.is_provisional_rule:
        print(
            f"drafted provisional check {bundle.stored_rel} "
            f"({check_id}, strategy={bundle.strategy}, input={plan.check_input}, layer={layer}, tool={plan.tool})"
        )
        if dry_summary:
            print(f"self-check on fixtures: {dry_summary} (record remains state=open)")
        elif not can_dry_run_locally:
            print(f"note: {plan.tool} is not on PATH; provisional rule.yml written without local dry-run")
        print(f"linked {incident_id} {check_id} -> {bundle.stored_rel} state={state}")
        if win_note:
            print(win_note)
        print(f"next: review {bundle.stored_rel} (adjust if overfit), then run: bin/mil prove {incident_id} --check-hunks")
    else:
        suffix = (
            f"; provisional candidate fell back to manual template: {fallback_reason}"
            if fallback_reason
            else ""
        )
        print(
            f"scaffolded {bundle.stored_rel} "
            f"({check_id}, input={plan.check_input}, layer={layer}, tool={plan.tool}{suffix})"
        )
        print(f"linked {incident_id} {check_id} -> {bundle.stored_rel} state={state}")
        if win_note:
            print(win_note)
        ext_linters = tc.available_external_linters()
        if plan.tool == "script" and plan.check_input == "tree" and ext_linters:
            print(
                f"note: external linters on PATH: {', '.join(ext_linters)} "
                "(pass --tool <linter> when generating a rule bundle)"
            )
        print(f"next: edit {bundle.stored_rel}, then run: bin/mil prove {incident_id}")
    return 0
