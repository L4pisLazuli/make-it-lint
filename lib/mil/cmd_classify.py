"""Classify or reclassify an open or human_review ledger record."""

from __future__ import annotations

import argparse
from pathlib import Path

from mil.draft import detect_toolchain, infer_plan
from mil.errors import MilError, mil_line
from mil.ledger import find_by_id, save_record_file, today
from mil.models import DECIDABLE, LAYERS, RECORD_ID_RE
from mil.paths import as_rel, mil_dir
from mil.routing import suggest_for_kind

CLASSIFY_EPILOG = """\
Set classification (kind, decidable) and stop layer on an open or
human_review record — for instance after capturing a raw failure with
`mil record --from-ci run.log`, or when a human_review record becomes
mechanically decidable on a second occurrence.

  --auto                infer kind, decidability, earliest stop layer, and tool from bad/ and good/
  --decidable no        moves open -> human_review
  --decidable yes       with --layer and --layer-reason, keeps/moves to open

usage:
  mil classify F-20260311-01 --auto
  mil classify F-20260311-01 --kind data --decidable yes --layer test --layer-reason "Unit test locks parser output."
  mil classify F-20260722-01 --kind boot --decidable partial --no-layer --layer-reason "External launcher setting."
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "classify",
        help="set kind, decidability, and stop layer on an open or human_review record",
        description="Set kind, decidability, and stop layer on an open or human_review record.",
        epilog=CLASSIFY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("incident", metavar="F-YYYYMMDD-NN")
    p.add_argument("--auto", action="store_true", help="infer kind, decidable, layer, and layer_reason from fixtures")
    p.add_argument("--kind", help="failure kind, e.g. lint, architecture, type, scope, data, boot")
    p.add_argument("--decidable", choices=list(DECIDABLE))
    p.add_argument("--layer", choices=list(LAYERS))
    p.add_argument("--no-layer", action="store_true", help="set layer: null (external cause)")
    p.add_argument("--layer-reason", help="why this stop layer (or why no layer can see it)")
    p.add_argument("--owner", help="update record owner")
    p.add_argument("--note", help="history note when state changes")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    if not mil_dir(root).is_dir():
        raise MilError(mil_line(root, "no .make-it-lint directory"))
    incident_id = args.incident
    if not RECORD_ID_RE.match(incident_id):
        raise MilError(mil_line("classify", f"{incident_id!r} is not a record ID (F-YYYYMMDD-NN)"))
    if args.layer and args.no_layer:
        raise MilError(mil_line("classify", "pass either --layer or --no-layer, not both"))
    if not args.auto and (not args.kind or not args.decidable):
        raise MilError(mil_line("classify", "--kind and --decidable are required (or pass --auto)"))

    found = find_by_id(root, incident_id)
    if not found:
        raise MilError(mil_line("classify", f"{incident_id}: not found"))
    rec_path, rec = found
    current = (rec.get("enforcement") or {}).get("state")
    if current not in {"open", "human_review"}:
        raise MilError(
            mil_line(
                rec_path,
                f"cannot reclassify in state {current!r}",
                "only open and human_review records may be classified via mil classify",
            )
        )

    auto_plan = None
    if args.auto:
        proof = rec.get("proof") if isinstance(rec.get("proof"), dict) else {}
        bad_dir = rec_path.parent / as_rel(str(proof.get("bad") or "bad/"))
        good_dir = rec_path.parent / as_rel(str(proof.get("good") or "good/"))
        if (
            not bad_dir.is_dir()
            or not good_dir.is_dir()
            or not any(c for c in bad_dir.iterdir() if not c.name.startswith("."))
            or not any(c for c in good_dir.iterdir() if not c.name.startswith("."))
        ):
            raise MilError(
                mil_line("classify", "--auto requires non-empty bad/ and good/ fixture directories on the record")
            )
        auto_plan = infer_plan(
            root,
            bad_dir,
            good_dir,
            toolchain=detect_toolchain(root),
            explicit_layer=args.layer,
        )

    kind = args.kind or (auto_plan.kind if auto_plan else "")
    decidable = args.decidable or (auto_plan.decidable if auto_plan else "")
    layer = None if args.no_layer else (args.layer or (auto_plan.layer if auto_plan else None))
    layer_reason = args.layer_reason or (auto_plan.layer_reason if auto_plan else None)

    if decidable == "no":
        if rec.get("check"):
            raise MilError(mil_line(rec_path, "record already has a check; cannot set --decidable no"))
        target_state = "human_review"
    else:
        if not layer and not args.no_layer:
            raise MilError(
                mil_line(
                    "classify",
                    f"--decidable {decidable} needs --layer <layer> or --no-layer",
                )
            )
        if not layer_reason:
            raise MilError(mil_line("classify", "--layer-reason is required when setting --layer or --no-layer"))
        target_state = "open"

    rec["classification"] = {
        "kind": kind,
        "decidable": decidable,
    }
    if decidable == "no" and not layer and not args.no_layer and not layer_reason:
        rec.pop("layer", None)
        rec.pop("layer_reason", None)
    else:
        rec["layer"] = None if args.no_layer else layer
        if layer_reason:
            rec["layer_reason"] = layer_reason
    if args.owner:
        rec["owner"] = args.owner

    if current != target_state:
        rec.setdefault("enforcement", {})["state"] = target_state
        hist = rec.setdefault("history", [])
        entry = {"state": target_state, "at": today()}
        if args.note:
            entry["note"] = args.note.strip()
        else:
            entry["note"] = "mil classify"
        hist.append(entry)

    save_record_file(rec_path, rec)
    if auto_plan:
        print(
            f"auto-selected: kind={kind} decidable={decidable} layer={rec.get('layer')} "
            f"tool={auto_plan.tool} input={auto_plan.check_input} (strategy={auto_plan.strategy})"
        )
    print(f"classified {incident_id} kind={kind} decidable={decidable} state={target_state} -> {rec_path}")
    if not layer and not args.no_layer:
        suggestion = suggest_for_kind(root, kind)
        if suggestion:
            print(suggestion)
    return 0
