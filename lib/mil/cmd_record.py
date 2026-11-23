"""Create a ledger record, or append an occurrence on a matching fingerprint."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from mil import yamlio
from mil.draft import detect_toolchain, infer_plan
from mil.errors import MilError, mil_line, rel_to
from mil.fixtures import is_multi_side
from mil.git_tree import materialize_ref
from mil.ledger import (
    default_promote_when,
    find_by_fingerprint,
    next_id,
    parse_date,
    record_dir,
    save_record_file,
    today,
)
from mil.models import CHECK_TOOLS, EVIDENCE_KINDS, FINGERPRINT_RE, LAYERS, stronger_layer
from mil.paths import is_within_root, mil_dir
from mil.routing import suggest_for_kind

RECORD_EPILOG = """\
mil record never prompts. Missing fields are an error. The same
--fingerprint appends an occurrence instead of creating a second record,
preserving --note and any new --bad-variant / --good-variant fixtures.

New records start in state=open (or human_review with --decidable no /
--human-review). Classification (--kind, --decidable, --layer,
--layer-reason) can be passed now or added later with `mil classify`.

--from-ci reads a local file only: a `gh run view --log-failed` dump, a
raw step log, or a YAML/JSON summary. It does not call GitHub.

--bad copies a file or tree-root directory into bad/. --good does the
same for good/. Named variants use --bad-variant NAME=PATH (with NAME=real
for the primary failure) and --good-variant NAME=PATH.

--bad and --good paths are inputs for recording only: the ledger gets its
own copy. Delete temporary source fixtures afterwards (or create them
outside the product repo) and never `git add` them; a broken fixture left
in the product tree is a real hit that trips `block` checks.

--layer is your explicit choice: when passed, mil prints no kind-based
layer suggestion. On a repeat fingerprint (an occurrence), --layer is not
applied to the existing record, and mil prints a note if it differs.

--auto inspects bad/ and good/ fixtures to select kind, decidability, the
earliest mechanical stop layer, and layer_reason automatically. In a git
repository, --bad-ref HEAD~1 --good-ref HEAD extracts changed files from two
commits directly into bad/ and good/.

examples:
  mil record --fingerprint lint:config-drift \\
      --title "Collect PR edited config timeout" \\
      --what "The PR overwrote timeout_seconds in config/settings.yaml." \\
      --source "review comment" --actor cloud-agent --reporter reviewer \\
      --bad failing-diff --good fixed-diff --auto
  mil record --fingerprint scope:path-outside-allowlist \\
      --title "Collect PR changed a config file outside its allowlist" \\
      --what "The PR was labeled collect but edited config/settings.yaml." \\
      --source "review comment" --actor cloud-agent --reporter reviewer \\
      --kind scope --decidable yes --layer ci \\
      --layer-reason "Needs PR kind and the set of changed paths." \\
      --bad failing-diff --good fixed-diff
  mil record --from-ci run.log --ref "run 1842"
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "record",
        help="add a ledger record, or an occurrence of a known failure",
        description="Add a ledger record, or an occurrence of a known failure.",
        epilog=RECORD_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("--title", help="one line: what went wrong")
    p.add_argument("--fingerprint", help="<kind>:<slug> that identifies this failure")
    p.add_argument("--source", help="where it was reported (review, issue, log)")
    p.add_argument("--actor", help="who made the mistake (agent type, e.g. cloud-agent)")
    p.add_argument("--reporter", help="who noticed it")
    p.add_argument("--what", dest="what_happened", help="what happened, in a few sentences")
    p.add_argument("--human-words", help="the reporter's own words, verbatim")
    p.add_argument(
        "--auto",
        action="store_true",
        help="infer kind, decidable, earliest stop layer, and layer_reason from bad/ and good/ fixtures",
    )
    p.add_argument("--kind")
    p.add_argument("--decidable", choices=["yes", "partial", "no"])
    p.add_argument("--layer", choices=list(LAYERS))
    p.add_argument("--no-layer", action="store_true", help="explicitly record layer: null (external cause)")
    p.add_argument("--layer-reason")
    p.add_argument("--owner", help="who looks after this record, e.g. @name")
    p.add_argument("--at", help="date of this occurrence, YYYY-MM-DD (default: today)")
    p.add_argument("--ref", help="reference for this occurrence, e.g. 'PR 12'")
    p.add_argument("--note", help="occurrence note, especially on a repeat failure")
    p.add_argument("--inventory-id")
    p.add_argument("--id", dest="incident_id", help="override generated F-YYYYMMDD-NN")
    p.add_argument(
        "--from-ci",
        dest="from_ci",
        metavar="LOG",
        help="draft the record from a local CI log file (no network)",
    )
    p.add_argument(
        "--bad",
        action="append",
        default=[],
        metavar="PATH",
        help="file, or tree-root directory whose contents become bad/ (repeatable)",
    )
    p.add_argument(
        "--good",
        action="append",
        default=[],
        metavar="PATH",
        help="file, or tree-root directory whose contents become good/ (repeatable)",
    )
    p.add_argument(
        "--bad-variant",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="named bad variant directory under bad/<NAME>/ (e.g. real=... or near-empty=...)",
    )
    p.add_argument(
        "--good-variant",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="named good variant directory under good/<NAME>/",
    )
    p.add_argument(
        "--bad-ref",
        dest="bad_ref",
        metavar="REF",
        help="git commit/ref holding the broken state (pair with --good-ref)",
    )
    p.add_argument(
        "--good-ref",
        dest="good_ref",
        metavar="REF",
        help="git commit/ref holding the fixed state (pair with --bad-ref)",
    )
    p.add_argument(
        "--paths",
        action="append",
        default=[],
        metavar="REL_PATH",
        help="relative file/dir path(s) to extract from --bad-ref/--good-ref (default: changed files)",
    )
    p.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="PATH",
        help="attach a local file as sha256-verified evidence on the record",
    )
    p.add_argument("--evidence-kind", choices=list(EVIDENCE_KINDS), default="log")
    p.add_argument("--evidence-note", help="short note for --evidence")
    p.add_argument("--check-id")
    p.add_argument("--check-tool", choices=list(CHECK_TOOLS))
    p.add_argument("--check-path")
    p.add_argument("--check-message")
    p.add_argument(
        "--human-review",
        action="store_true",
        help="start in human_review (also implied by --decidable no)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    mil_dir(root).mkdir(parents=True, exist_ok=True)
    if args.layer and args.no_layer:
        raise MilError(mil_line("record", "pass either --layer or --no-layer, not both"))
    if bool(args.bad_ref) != bool(args.good_ref):
        raise MilError(mil_line("record", "pass both --bad-ref and --good-ref together"))
    if args.paths and not args.bad_ref:
        raise MilError(mil_line("record", "--paths requires --bad-ref and --good-ref"))
    fields = _fields_from_args(args)
    if args.from_ci:
        ci_fields = read_from_ci(Path(args.from_ci))
        for key, value in ci_fields.items():
            fields.setdefault(key, value)
    fingerprint = fields.get("fingerprint")
    if not fingerprint:
        raise MilError(mil_line("record", "--fingerprint is required (or pass --from-ci with a parseable log)"))
    if not FINGERPRINT_RE.match(str(fingerprint)):
        raise MilError(
            mil_line(
                "record",
                f"--fingerprint {fingerprint!r} must look like <kind>:<slug>, lower case, no spaces",
            )
        )
    if args.at:
        d = parse_date(args.at)
        if not d:
            raise MilError(mil_line("record", f"--at {args.at!r} is not a YYYY-MM-DD date"))
        at = d.isoformat()
    else:
        at = fields.get("at") or today()
    existing = find_by_fingerprint(root, fingerprint)
    ref = fields.get("ref")
    note = fields.get("note")
    if existing:
        return _append_existing(root, existing, args, fields, at, ref, note)

    missing = [name for name in ("title", "source", "actor", "reporter", "what_happened") if not fields.get(name)]
    if missing:
        flags = ", ".join("--" + name.replace("_", "-") if name != "what_happened" else "--what" for name in missing)
        raise MilError(mil_line("record", f"missing {flags}"))
    if not args.auto:
        _validate_layer_fields(fields)
    incident_id = fields.get("incident_id") or next_id(root, at)
    dest = record_dir(root, incident_id)
    if dest.exists() and any(dest.iterdir()):
        raise MilError(mil_line("record", f"{dest} already exists"))
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "bad").mkdir(exist_ok=True)
    (dest / "good").mkdir(exist_ok=True)
    auto_plan = None
    try:
        copied = _copy_into(dest / "bad", args.bad or [], "bad")
        copied += _copy_into(dest / "good", args.good or [], "good")
        copied += _copy_variants(dest / "bad", args.bad_variant or [], "bad")
        copied += _copy_variants(dest / "good", args.good_variant or [], "good")
        if args.bad_ref and args.good_ref:
            copied += _copy_from_git_refs(
                root,
                dest / "bad",
                dest / "good",
                bad_ref=args.bad_ref,
                good_ref=args.good_ref,
                paths=args.paths or [],
            )
        ev_items, ev_lines = _attach_evidence(
            root, dest, args.evidence or [], kind=args.evidence_kind, note=args.evidence_note
        )
        copied += ev_lines
        if args.auto and not (fields.get("human_review") or fields.get("decidable") == "no" or fields.get("no_layer")):
            if not any(c for c in (dest / "bad").iterdir() if not c.name.startswith(".")) or not any(
                c for c in (dest / "good").iterdir() if not c.name.startswith(".")
            ):
                raise MilError(
                    mil_line(
                        "record",
                        "--auto requires non-empty bad/ and good/ fixtures (--bad/--good or --bad-ref/--good-ref)",
                    )
                )
            auto_plan = infer_plan(
                root,
                dest / "bad",
                dest / "good",
                toolchain=detect_toolchain(root),
                explicit_layer=fields.get("layer"),
                explicit_tool=fields.get("check_tool"),
            )
            fields.setdefault("kind", auto_plan.kind)
            fields.setdefault("decidable", auto_plan.decidable)
            fields.setdefault("layer", auto_plan.layer)
            fields.setdefault("layer_reason", auto_plan.layer_reason)
        _validate_layer_fields(fields)
        rec = _new_record(incident_id, fields, at, ref, note)
        if ev_items:
            rec["evidence"] = ev_items
    except MilError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    path = dest / "record.yaml"
    save_record_file(path, rec)
    print(f"wrote {rec['id']} state={rec['enforcement']['state']} -> {path}")
    for line in copied:
        print(line)
    if auto_plan:
        print(
            f"auto-selected: kind={rec.get('classification', {}).get('kind')} "
            f"decidable={rec.get('classification', {}).get('decidable')} "
            f"layer={rec.get('layer')} tool={auto_plan.tool} input={auto_plan.check_input} "
            f"(strategy={auto_plan.strategy})"
        )
    elif fields.get("kind") and not args.layer and not args.no_layer:
        suggestion = suggest_for_kind(root, fields.get("kind"))
        if suggestion:
            print(suggestion)
    return 0


def _append_existing(
    root: Path,
    existing: tuple[Path, dict[str, Any]],
    args: argparse.Namespace,
    fields: dict[str, Any],
    at: str,
    ref: str | None,
    note: str | None,
) -> int:
    path, rec = existing
    dest = path.parent
    occ = list(rec.get("occurrences") or [])
    entry: dict[str, Any] = {"at": at}
    if ref:
        entry["ref"] = ref
    occ_note = note or (fields.get("what_happened") if not args.from_ci else None)
    if occ_note:
        entry["note"] = str(occ_note).strip()
    occ.append(entry)
    rec["occurrences"] = occ

    copied: list[str] = []
    if args.bad_variant:
        _promote_flat_to_real(dest / "bad", dest / "good")
        copied += _copy_variants(dest / "bad", args.bad_variant, "bad")
    if args.good_variant:
        _promote_flat_to_real(dest / "bad", dest / "good")
        copied += _copy_variants(dest / "good", args.good_variant, "good")
    if args.evidence:
        ev_items, ev_lines = _attach_evidence(
            root, dest, args.evidence, kind=args.evidence_kind, note=args.evidence_note
        )
        rec.setdefault("evidence", []).extend(ev_items)
        copied += ev_lines

    save_record_file(path, rec)
    count = len(occ)
    print(f"appended occurrence {count} to {rec.get('id')} -> {path}")
    for line in copied:
        print(line)
    if args.layer and args.layer != rec.get("layer"):
        print(
            f"note: --layer {args.layer} is not applied to an existing record; "
            f"{rec.get('id')} keeps layer={rec.get('layer')}"
        )
    if fields.get("kind") and not args.layer and not args.no_layer:
        suggestion = suggest_for_kind(root, fields.get("kind"))
        if suggestion:
            print(suggestion)
    if count == 2:
        print("this fingerprint has occurred twice")
        _suggest_layer_raise(rec)
    elif count > 2:
        print(f"this fingerprint has recurred {count} times; the layer may still be too weak")
        _suggest_layer_raise(rec)
    return 0


def _promote_flat_to_real(bad: Path, good: Path) -> None:
    """If bad/ holds a single flat fixture, move it into bad/real/ (and good/ into good/real/)."""
    for side in (bad, good):
        if not side.is_dir() or is_multi_side(side):
            continue
        entries = [p for p in side.iterdir() if not p.name.startswith(".")]
        if not entries:
            continue
        real = side / "real"
        tmp = side.parent / f".promote-{side.name}"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        for item in entries:
            shutil.move(str(item), str(tmp / item.name))
        shutil.move(str(tmp), str(real))


def _validate_layer_fields(fields: dict[str, Any]) -> None:
    layer = fields.get("layer")
    no_layer = bool(fields.get("no_layer"))
    reason = fields.get("layer_reason")
    if layer and not reason:
        raise MilError(mil_line("record", f"--layer {layer} needs --layer-reason: say why this layer"))
    if no_layer and not reason:
        raise MilError(mil_line("record", "--no-layer needs --layer-reason: say why no layer can stop it"))
    human_review = bool(fields.get("human_review")) or fields.get("decidable") == "no"
    if fields.get("kind") and fields.get("decidable") and not human_review and not reason:
        raise MilError(
            mil_line(
                "record",
                "classification with --kind and --decidable needs --layer with --layer-reason, "
                "or --no-layer with --layer-reason when no layer can stop it",
            )
        )


def _copy_into(dest: Path, sources: list[str], label: str) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for src in sources:
        p = Path(src)
        if not p.exists():
            raise MilError(mil_line("record", f"{p}: no such file"))
        if p.is_dir():
            names = []
            for item in sorted(p.iterdir()):
                target = dest / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                    names.append(item.name + "/")
                else:
                    shutil.copy2(item, target)
                    names.append(item.name)
            shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
            lines.append(f"{label}/ <- contents of {src}: {shown or '(empty)'}")
        else:
            shutil.copy2(p, dest / p.name)
            lines.append(f"{label}/{p.name} <- {src}")
    return lines


def _copy_variants(side_dir: Path, specs: list[str], side_name: str) -> list[str]:
    lines: list[str] = []
    for spec in specs:
        if "=" not in spec:
            raise MilError(mil_line("record", f"variant {spec!r} must look like NAME=PATH"))
        name, raw_path = spec.split("=", 1)
        name = name.strip()
        if not name or "/" in name or "\\" in name or name in {".", "..", "base", "head"}:
            raise MilError(mil_line("record", f"invalid variant name {name!r}"))
        lines.extend(_copy_into(side_dir / name, [raw_path], f"{side_name}/{name}"))
    return lines


def _tree_rel_files(tree: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in sorted(tree.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(tree).parts
        if rel_parts and rel_parts[0] in {".make-it-lint", ".git"}:
            continue
        out[p.relative_to(tree).as_posix()] = p
    return out


def _copy_from_git_refs(
    root: Path,
    dest_bad: Path,
    dest_good: Path,
    *,
    bad_ref: str,
    good_ref: str,
    paths: list[str],
) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="mil-ref-") as tmp:
        tbad = Path(tmp) / "bad"
        tgood = Path(tmp) / "good"
        materialize_ref(root, bad_ref, tbad)
        materialize_ref(root, good_ref, tgood)
        b_files = _tree_rel_files(tbad)
        g_files = _tree_rel_files(tgood)
        all_rels = set(b_files) | set(g_files)

        if paths:
            chosen: set[str] = set()
            for raw in paths:
                norm = raw.replace("\\", "/").strip("/")
                if not norm or ".." in norm.split("/") or norm.startswith(".make-it-lint"):
                    raise MilError(mil_line("record", f"invalid --paths entry {raw!r}"))
                matched = {r for r in all_rels if r == norm or r.startswith(norm + "/")}
                if not matched:
                    raise MilError(mil_line("record", f"--paths {raw!r}: not found in {bad_ref} or {good_ref}"))
                chosen.update(matched)
            selected = sorted(chosen)
        else:
            selected = sorted(
                r
                for r in all_rels
                if r not in b_files or r not in g_files or b_files[r].read_bytes() != g_files[r].read_bytes()
            )
            if not selected:
                raise MilError(mil_line("record", f"no file changes found between {bad_ref} and {good_ref}"))

        has_visible_bad = any(
            not r.split("/")[0].startswith(".") for r in selected if r in b_files
        )
        has_visible_good = any(
            not r.split("/")[0].startswith(".") for r in selected if r in g_files
        )
        if not has_visible_bad or not has_visible_good:
            shared_visible = sorted(
                r
                for r in (set(b_files) & set(g_files)) - set(selected)
                if not r.split("/")[0].startswith(".")
            )
            if shared_visible:
                selected = sorted(set(selected) | {shared_visible[0]})

        copied_bad: list[str] = []
        copied_good: list[str] = []
        for rel in selected:
            if rel in b_files:
                target = dest_bad / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(b_files[rel], target)
                copied_bad.append(rel)
            if rel in g_files:
                target = dest_good / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(g_files[rel], target)
                copied_good.append(rel)

    lines: list[str] = []
    shown_b = ", ".join(copied_bad[:6]) + (", ..." if len(copied_bad) > 6 else "")
    shown_g = ", ".join(copied_good[:6]) + (", ..." if len(copied_good) > 6 else "")
    lines.append(f"bad/ <- {bad_ref}: {shown_b or '(none)'}")
    lines.append(f"good/ <- {good_ref}: {shown_g or '(none)'}")
    return lines


def _attach_evidence(
    root: Path, dest: Path, paths: list[str], *, kind: str, note: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    lines: list[str] = []
    for raw in paths:
        src = Path(raw)
        if not src.is_file():
            raise MilError(mil_line("record", f"--evidence {src}: file not found"))
        resolved = src.resolve()
        if is_within_root(root, resolved):
            stored = resolved
        else:
            ev_dir = dest / "evidence"
            ev_dir.mkdir(parents=True, exist_ok=True)
            stored = ev_dir / src.name
            shutil.copy2(src, stored)
        digest = hashlib.sha256(stored.read_bytes()).hexdigest()
        rel = rel_to(root, stored).replace("\\", "/")
        item: dict[str, Any] = {"kind": kind, "path": rel, "sha256": digest}
        if note:
            item["note"] = note
        items.append(item)
        lines.append(f"evidence <- {rel} (sha256:{digest[:12]})")
    return items, lines


def _new_record(
    incident_id: str, fields: dict[str, Any], at: str, ref: str | None, note: str | None
) -> dict[str, Any]:
    origin: dict[str, Any] = {
        "source": fields["source"],
        "actor": fields["actor"],
        "reporter": fields["reporter"],
    }
    if fields.get("inventory_id"):
        origin["inventory_id"] = fields["inventory_id"]
    occurrence: dict[str, Any] = {"at": at}
    if ref:
        occurrence["ref"] = ref
    if note:
        occurrence["note"] = note

    decidable = fields.get("decidable")
    human_review = bool(fields.get("human_review")) or decidable == "no"
    state = "human_review" if human_review else "open"
    history = [{"state": state, "at": at}]

    layer = fields.get("layer")
    layer_reason = fields.get("layer_reason")

    rec: dict[str, Any] = {
        "id": incident_id,
        "title": fields["title"],
        "origin": origin,
        "what_happened": _ensure_trailing_nl(fields["what_happened"])
        if "\n" in fields["what_happened"]
        else fields["what_happened"],
        "human_words": fields.get("human_words"),
        "occurrences": [occurrence],
        "fingerprint": fields["fingerprint"],
        "enforcement": {
            "state": state,
            "promote_when": default_promote_when(),
        },
        "owner": fields.get("owner") or "unassigned",
        "history": history,
    }
    if fields.get("kind") or decidable or human_review:
        rec["classification"] = {
            "kind": fields.get("kind") or ("judgment" if human_review else "unclassified"),
            "decidable": "no" if human_review and not decidable else (decidable or "partial"),
        }
    if layer:
        rec["layer"] = layer
        rec["layer_reason"] = layer_reason
    elif layer_reason:
        rec["layer"] = None
        rec["layer_reason"] = layer_reason

    if (
        fields.get("check_id")
        and fields.get("check_tool")
        and fields.get("check_path")
        and fields.get("check_message")
    ):
        if state == "human_review":
            raise MilError(mil_line("record", "human_review records cannot carry a check"))
        rec["check"] = {
            "id": fields["check_id"],
            "tool": fields["check_tool"],
            "path": fields["check_path"],
            "message": fields["check_message"],
        }
        rec["proof"] = {"bad": "bad/", "good": "good/", "last_result": None}
    return rec


def _suggest_layer_raise(rec: dict[str, Any]) -> None:
    print(raise_layer_advice(rec))


def raise_layer_advice(rec: dict[str, Any]) -> str:
    state = (rec.get("enforcement") or {}).get("state")
    layer = rec.get("layer")
    if state == "human_review":
        return (
            "it is in human_review. a second occurrence is the cue to look again: "
            "if a machine can decide it now, run mil classify to move it to open"
        )
    if state == "open" and not rec.get("check"):
        if "layer" in rec and layer is None:
            return (
                "no layer could stop it last time. look again for one, and say why "
                "in layer_reason if there is still none"
            )
        return "there is no check yet. classify it (if needed) and write the check"
    stronger = stronger_layer(layer) if isinstance(layer, str) else None
    if layer in LAYERS:
        if stronger is None:
            return (
                "it is already at the strongest layer (architecture). find out why "
                "the change did not stop it"
            )
        return (
            f"the {layer} layer did not stop it. raise one level "
            f"(stronger stop, toward architecture): change layer from {layer} to {stronger}. "
            f"strongest to weakest: {', '.join(LAYERS)}"
        )
    return (
        "consider a stronger layer (toward architecture). "
        f"strongest to weakest: {', '.join(LAYERS)}"
    )


_CI_PREFIX_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_CI_ERROR_RE = re.compile(
    r"(?i)(\berror\b|\bfailed\b|\bfailure\b|\bfatal\b|\bassert(ion)?\b|^FAIL\b|Traceback)"
)
_CI_HIT_RE = re.compile(r"^[^\s:]+:\d+(:\d+)?: ")
_CI_NOISE_RE = re.compile(r"^(##\[(group|endgroup|debug)\]|::(group|endgroup|debug)::)")


def parse_ci_log(text: str) -> tuple[str | None, list[str]]:
    job = None
    lines = []
    for raw in text.splitlines():
        parts = raw.split("\t")
        if len(parts) >= 3:
            job = job or parts[0].strip() or None
            raw = "\t".join(parts[2:])
        line = _CI_PREFIX_RE.sub("", raw).rstrip()
        line = line.replace("##[error]", "error: ")
        if line and not _CI_NOISE_RE.match(line):
            lines.append(line)
    errors = [ln for ln in lines if _CI_HIT_RE.match(ln) or _CI_ERROR_RE.search(ln)]
    if not errors and lines:
        errors = [lines[-1]]
    return job, errors


def ci_fingerprint(first_error: str) -> str:
    s = re.sub(r"\d+", "", first_error.lower())
    s = re.sub(r"[^a-z]+", "-", s).strip("-")
    slug = s[:60].rstrip("-") or "unknown"
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug).strip("-") or "unknown"
    return "ci:" + slug


def read_from_ci(path: Path) -> dict[str, Any]:
    if re.fullmatch(r"[0-9]+", str(path)):
        raise MilError(
            mil_line(
                "record",
                "--from-ci does not fetch GitHub run ids (mil uses no network). "
                "pass a local log or YAML/JSON file",
            )
        )
    if not path.is_file():
        raise MilError(mil_line("record", f"--from-ci file not found: {path}"))
    text = path.read_text(encoding="utf-8", errors="replace")
    parsed = _try_structured(path, text)
    if parsed:
        return parsed
    return _from_plain_log(path, text)


def _try_structured(path: Path, text: str) -> dict[str, Any] | None:
    data: Any = None
    if path.suffix in {".json"}:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
    else:
        try:
            data = yamlio.load(text)
        except yamlio.YAMLError:
            data = None
    if not isinstance(data, dict):
        return None
    interesting = {
        "title",
        "what_happened",
        "fingerprint",
        "source",
        "actor",
        "reporter",
        "log",
        "log_excerpt",
    }
    if not interesting.intersection(data):
        return None
    log = data.get("what_happened") or data.get("log_excerpt") or data.get("log") or ""
    title = data.get("title") or _first_line(str(log)) or f"CI failure in {path.name}"
    fingerprint = data.get("fingerprint") or "ci:" + _slug(title)
    out = {
        "title": title,
        "what_happened": str(log) if log else f"CI log {path} reported a failure.",
        "fingerprint": fingerprint,
        "source": data.get("source") or f"ci-log:{path.name}",
        "actor": data.get("actor") or "ci",
        "reporter": data.get("reporter") or "ci",
        "ref": data.get("ref") or data.get("run") or data.get("job"),
        "kind": data.get("kind"),
        "decidable": data.get("decidable"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _from_plain_log(path: Path, text: str) -> dict[str, Any]:
    job, errors = parse_ci_log(text)
    marker = errors[0] if errors else None
    if marker is None:
        for line in reversed(text.splitlines()):
            if line.strip():
                marker = line.strip()
                break
    title = (marker or f"CI failure captured from {path.name}")[:120]
    excerpt = "\n".join(errors[:40]).strip() if errors else text
    if not excerpt.endswith("\n"):
        excerpt += "\n"
    return {
        "title": title,
        "what_happened": excerpt,
        "fingerprint": ci_fingerprint(marker or title),
        "source": f"ci-log:{path.name}",
        "actor": "ci",
        "reporter": "ci",
        "ref": job or path.name,
    }


def _fields_from_args(args: argparse.Namespace) -> dict[str, Any]:
    names = [
        "title",
        "fingerprint",
        "source",
        "actor",
        "reporter",
        "what_happened",
        "human_words",
        "kind",
        "decidable",
        "layer",
        "no_layer",
        "layer_reason",
        "owner",
        "at",
        "ref",
        "note",
        "inventory_id",
        "incident_id",
        "check_id",
        "check_tool",
        "check_path",
        "check_message",
        "human_review",
    ]
    return {name: getattr(args, name) for name in names if getattr(args, name, None) not in (None, False)}


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "failure"


def _ensure_trailing_nl(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"
