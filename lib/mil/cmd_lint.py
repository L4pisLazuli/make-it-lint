"""Self-check the ledger, waivers, check metadata, and config."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from mil import CONTRACT, yamlio
from mil.checks import (
    CheckError,
    check_entries,
    check_executable_issue,
    discover_checks,
    git_non_exec_checks,
    resolve_check_program,
)
from mil.errors import mil_line, rel_to
from mil.fixtures import (
    is_multi_fixture,
    is_multi_side,
    list_bad_variants,
    list_good_variants,
    stray_multi_files,
)
from mil.human_ok import approver_looks_like_agent
from mil.ledger import parse_date, today
from mil.models import (
    LAYERS,
    PROVEN_OR_LATER,
    PROVEN_RESULT,
    RECORD_ID_RE,
    RUNS_ON_PRODUCT,
    is_proven_result,
    transitions_ok,
)
from mil.paths import (
    as_rel,
    checks_dir,
    config_path,
    contract_doc_path,
    install_root,
    is_within_root,
    ledger_dir,
    mil_dir,
    waivers_path,
)
from mil.schema import SchemaIssue, load_schema, validate


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "lint",
        help="check the ledger, checks, waivers and config against the contract",
        description="Check the ledger, checks, waivers and config against the contract.",
        epilog="Exits 1 if any problem is found. Warnings do not fail.\n\nexamples:\n  mil lint",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    if not mil_dir(root).is_dir():
        print(mil_line(root, "no .make-it-lint directory"), file=sys.stderr)
        return 2
    issues: list[str] = []
    warnings: list[str] = []
    issues.extend(_lint_config(root))
    issues.extend(_lint_contract_docs())
    records, load_issues = _load_records(root)
    issues.extend(load_issues)
    rec_issues, rec_warnings = _lint_records(root, records)
    issues.extend(rec_issues)
    warnings.extend(rec_warnings)
    checks, check_issues = _collect_checks(root)
    issues.extend(check_issues)
    issues.extend(_lint_checks(root, records, checks))
    n_checks = len(checks)
    check_states = _effective_check_states(records, checks)
    n_waivers, waiver_issues, waiver_warnings = _lint_waivers(root, check_states)
    issues.extend(waiver_issues)
    warnings.extend(waiver_warnings)
    for item in issues:
        print(item)
    for item in warnings:
        print(item)
    if issues:
        print(f"mil lint: {len(issues)} error(s)")
        return 1
    waiver_word = "waiver" if n_waivers == 1 else "waivers"
    print(
        f"mil lint: ok ({len(records)} records, {n_checks} checks, {n_waivers} {waiver_word}, {CONTRACT})"
    )
    return 0


def _load_records(root: Path) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    issues: list[str] = []
    out: list[tuple[Path, dict[str, Any]]] = []
    base = ledger_dir(root)
    if not base.is_dir():
        issues.append(_err(root, base, "missing"))
        return out, issues
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        rec = child / "record.yaml"
        if not rec.is_file():
            if any(child.iterdir()):
                issues.append(_err(root, child, "directory has no record.yaml"))
            continue
        try:
            from mil.ledger import load_record_file

            out.append((rec, load_record_file(rec)))
        except yamlio.YAMLError as exc:
            issues.append(_err(root, rec, str(exc)))
    return out, issues


def _lint_config(root: Path) -> list[str]:
    path = config_path(root)
    if not path.is_file():
        return [_err(root, path, "missing (copy template/.make-it-lint/config.yaml)")]
    try:
        data = yamlio.load_path(str(path))
    except yamlio.YAMLError as exc:
        return [_err(root, path, str(exc))]
    if not isinstance(data, dict):
        return [_err(root, path, "must be a mapping")]
    out = []
    if data.get("contract") != CONTRACT:
        out.append(
            _err(
                root,
                path,
                f"contract is {data.get('contract')!r}; this mil speaks {CONTRACT}",
            )
        )
        data = dict(data)
        data["contract"] = CONTRACT
    for issue in validate(data, load_schema("config.schema.json")):
        out.append(_fmt(root, path, issue))
    return out


def _lint_contract_docs() -> list[str]:
    issues = []
    schema_const = load_schema("config.schema.json")["properties"]["contract"]["const"]
    if schema_const != CONTRACT:
        issues.append(
            mil_line(
                install_root() / "schema" / "config.schema.json",
                f"const {schema_const!r} != mil.CONTRACT {CONTRACT!r}",
            )
        )
    contract_md = contract_doc_path()
    if contract_md.is_file():
        text = contract_md.read_text(encoding="utf-8")
        if CONTRACT not in text:
            issues.append(mil_line(contract_md, f"does not mention {CONTRACT}"))
    return issues


def _lint_records(root: Path, records: list[tuple[Path, dict[str, Any]]]) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    schema = load_schema("incident.schema.json")
    kinds = _configured_kinds(root)
    seen_ids: dict[str, Path] = {}
    seen_fp: dict[str, Path] = {}
    for path, rec in records:
        for issue in validate(rec, schema):
            issues.append(_fmt(root, path, issue))
        incident_id = rec.get("id")
        d = path.parent
        if not RECORD_ID_RE.match(d.name):
            issues.append(_err(root, d, "ledger directories are named F-YYYYMMDD-NN"))
        elif incident_id != d.name:
            issues.append(_err(root, path, f"id {incident_id!r} does not match its directory {d.name}"))
        else:
            y, m, day = d.name[2:6], d.name[6:8], d.name[8:10]
            if parse_date(f"{y}-{m}-{day}") is None:
                issues.append(_err(root, d, f"{d.name} does not contain a real date"))
        if isinstance(incident_id, str):
            if incident_id in seen_ids:
                issues.append(_err(root, path, f"duplicate id {incident_id!r} (also {seen_ids[incident_id]})"))
            else:
                seen_ids[incident_id] = path
        fingerprint = rec.get("fingerprint")
        if isinstance(fingerprint, str):
            if fingerprint in seen_fp:
                issues.append(
                    _err(root, path, f"duplicate fingerprint {fingerprint!r} (also {seen_fp[fingerprint]})")
                )
            else:
                seen_fp[fingerprint] = path

        cls = rec.get("classification")
        kind = cls.get("kind") if isinstance(cls, dict) else None
        if kinds is not None and kind is not None and kind not in kinds:
            warnings.append(
                mil_line(
                    rel_to(root, path),
                    f"classification.kind {kind!r} is not in config ledger.kinds; using it anyway",
                )
            )
        issues.extend(_lint_history(root, path, rec))
        for o in rec.get("occurrences") or []:
            if isinstance(o, dict) and o.get("at") is not None and parse_date(o.get("at")) is None:
                issues.append(_err(root, path, f"occurrences: {o.get('at')!r} is not a real date"))

        current = (rec.get("enforcement") or {}).get("state")
        if "layer" in rec and rec.get("layer") is None and not rec.get("layer_reason"):
            issues.append(_err(root, path, "layer is null; say in layer_reason why no layer can stop it"))
        if current == "human_review":
            if isinstance(cls, dict) and cls.get("decidable") == "yes":
                warnings.append(
                    mil_line(
                        rel_to(root, path),
                        "human_review but decidable is yes; a machine may be able to check it",
                    )
                )
            if rec.get("check"):
                issues.append(_err(root, path, "human_review records must not carry a check"))

        chk = rec.get("check")
        has_check_field = "check" in rec
        if current in PROVEN_OR_LATER:
            if not isinstance(cls, dict):
                issues.append(_err(root, path, f"state {current} needs classification (kind, decidable)"))
            if rec.get("layer") not in LAYERS:
                issues.append(_err(root, path, f"state {current} needs a layer"))
            if not rec.get("layer_reason"):
                issues.append(_err(root, path, f"state {current} needs layer_reason"))
            if not isinstance(chk, dict):
                issues.append(_err(root, path, f"state {current} needs a check (id, tool, path, message)"))

        if isinstance(chk, dict) or (has_check_field and chk is None and current != "open"):
            if chk is None:
                issues.append(_err(root, path, f"state {current} needs a check (id, tool, path, message)"))
            else:
                rel = chk.get("path")
                if isinstance(rel, str) and rel:
                    if not (root / rel).exists():
                        issues.append(_err(root, path, f"check.path {rel} does not exist"))
                    else:
                        target_check = root / rel
                        try:
                            resolve_check_program(target_check)
                        except CheckError as exc:
                            detail = str(exc).removeprefix(f"{target_check}: ")
                            issues.append(_err(root, path, f"check.path {rel}: {detail}"))
                if current in PROVEN_OR_LATER or current == "open":
                    proof = rec.get("proof") or {}
                    bad = d / as_rel(proof.get("bad") or "bad/")
                    good = d / as_rel(proof.get("good") or "good/")
                    issues.extend(_lint_fixture_pair(root, path, bad, good, current or "open"))

        if current in PROVEN_OR_LATER:
            lr = (rec.get("proof") or {}).get("last_result")
            if not is_proven_result(lr):
                issues.append(
                    _err(
                        root,
                        path,
                        f"state {current} needs proof.last_result {PROVEN_RESULT!r} "
                        f"or 'red-on-bad N/N, green-on-good M/M', "
                        f"found {lr!r} (run mil prove {d.name})",
                    )
                )

        ev_issues, ev_warnings = _lint_evidence(root, path, rec)
        issues.extend(ev_issues)
        warnings.extend(ev_warnings)
        warnings.extend(_lint_covers(root, path, rec))
        decidable = cls.get("decidable") if isinstance(cls, dict) else None
        if decidable == "no" and current not in {"human_review", "open", "retired"}:
            issues.append(
                _err(root, path, "decidable no must stay in human_review (or retired)")
            )
    return issues, warnings


def _lint_fixture_pair(root: Path, rec_path: Path, bad: Path, good: Path, state: str) -> list[str]:
    issues: list[str] = []
    for p in (bad, good):
        if not p.is_dir():
            issues.append(_err(root, rec_path, f"state {state} needs {rel_to(root, p)}"))
        elif not any(c for c in p.iterdir() if not c.name.startswith(".")):
            issues.append(_err(root, p, "is empty"))
    if bad.is_dir() and good.is_dir() and is_multi_fixture(bad, good):
        if is_multi_side(bad):
            real = bad / "real"
            if not real.is_dir() or not any(real.rglob("*")):
                issues.append(_err(root, rec_path, f"state {state} needs {rel_to(root, real)}"))
        for stray in stray_multi_files(bad):
            issues.append(_err(root, stray, "stray file in multi-variant bad/; move inside a variant directory"))
        for stray in stray_multi_files(good):
            issues.append(_err(root, stray, "stray file in multi-variant good/; move inside a variant directory"))
        if not list_bad_variants(bad, multi=True):
            issues.append(_err(root, rec_path, f"state {state} needs a named directory under {rel_to(root, bad)}"))
        if not list_good_variants(good, multi=True):
            issues.append(_err(root, rec_path, f"state {state} needs a named directory under {rel_to(root, good)}"))
    return issues


def _lint_history(root: Path, path: Path, rec: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    hist = rec.get("history")
    state = (rec.get("enforcement") or {}).get("state")
    if not isinstance(hist, list) or not hist:
        return issues
    steps = [h for h in hist if isinstance(h, dict)]
    if len(steps) != len(hist):
        return issues
    states = [item.get("state") for item in steps if isinstance(item.get("state"), str)]
    msg = transitions_ok(states)
    if msg:
        issues.append(_err(root, path, msg))
    prev_date = None
    prev_state = None
    for h in steps:
        d = parse_date(h.get("at"))
        if h.get("at") is not None and d is None:
            issues.append(_err(root, path, f"history: {h.get('at')!r} is not a real date"))
        if d and prev_date and d < prev_date:
            issues.append(_err(root, path, f"history: {h.get('at')} is earlier than the step before it"))
        prev_date = d or prev_date
        by = h.get("by")
        if isinstance(by, str) and approver_looks_like_agent(by):
            issues.append(_err(root, path, f"history: by {by!r} names an agent; approvals are human-only"))
        step_state = h.get("state")
        if step_state in RUNS_ON_PRODUCT:
            approved = h.get("approved_by")
            if isinstance(approved, str) and approver_looks_like_agent(approved):
                issues.append(
                    _err(root, path, f"history {step_state}: approved_by looks like an agent; promotion is a human-only path")
                )
        from_state = h.get("from_state")
        if from_state is not None and prev_state is not None and from_state != prev_state:
            issues.append(
                _err(
                    root,
                    path,
                    f"history {step_state}: from_state is {from_state!r} but the previous step is {prev_state!r}",
                )
            )
        prev_state = step_state if isinstance(step_state, str) else prev_state
    if steps[-1].get("state") != state:
        issues.append(
            _err(
                root,
                path,
                f"enforcement.state is {state!r} but the last history step is {steps[-1].get('state')!r}",
            )
        )
    return issues


def _collect_checks(root: Path) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    base = checks_dir(root)
    if not base.is_dir():
        return [], [_err(root, base, "missing")]
    found, errors = discover_checks(root)
    issues = [
        mil_line(str(exc)) if isinstance(exc, CheckError) else _err(root, child, str(exc))
        for child, exc in errors
    ]
    return found, issues


def _effective_check_states(
    records: list[tuple[Path, dict[str, Any]]],
    checks: list[tuple[Path, dict[str, Any]]],
) -> dict[str, str]:
    by_incident = {rec.get("id"): rec for _, rec in records}
    out: dict[str, str] = {}
    for _, meta in checks:
        cid = meta.get("id")
        if not isinstance(cid, str):
            continue
        incident = meta.get("incident")
        if incident and incident in by_incident:
            state = str((by_incident[incident].get("enforcement") or {}).get("state") or "")
        else:
            state = str(((meta.get("enforcement") or {}).get("state")) or "shadow")
        out[cid] = state
    return out


def _lint_waivers(
    root: Path, check_states: dict[str, str]
) -> tuple[int, list[str], list[str]]:
    path = waivers_path(root)
    if not path.is_file():
        return 0, [_err(root, path, "missing (an empty list [] is fine)")], []
    try:
        data = yamlio.load_path(str(path))
    except yamlio.YAMLError as exc:
        return 0, [_err(root, path, str(exc))], []
    issues = [_fmt(root, path, issue) for issue in validate(data, load_schema("waiver.schema.json"))]
    warnings: list[str] = []
    if not isinstance(data, list):
        return 0, issues, warnings
    now = parse_date(today()) or datetime.date.today()
    per_check: dict[str, int] = defaultdict(int)
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        loc = f"[{i}]"
        expires = item.get("expires")
        d = parse_date(expires)
        if expires is not None and d is None:
            issues.append(_err(root, path, f"{loc} expires {expires!r} is not a real date"))
        elif d is not None and d < now:
            issues.append(
                _err(
                    root,
                    path,
                    f"{loc} waiver for {item.get('check')} expired on {expires}; "
                    "remove it or renew it in a reviewed PR",
                )
            )
        cid = item.get("check")
        if isinstance(cid, str) and cid:
            if cid not in check_states:
                issues.append(
                    _err(
                        root,
                        path,
                        f"{loc} waiver names check {cid}, which is not in .make-it-lint/checks/",
                    )
                )
            elif check_states[cid] == "retired":
                warnings.append(
                    mil_line(
                        rel_to(root, path),
                        f"{loc} waiver names retired check {cid}; remove stale waiver",
                    )
                )
            per_check[cid] += 1
        approved = item.get("approved_by")
        if isinstance(approved, str) and approver_looks_like_agent(approved):
            issues.append(_err(root, path, f"{loc} approved_by looks like an agent; waivers are a human-only path"))
    for cid, n in sorted(per_check.items()):
        if n >= 3:
            warnings.append(
                mil_line(
                    rel_to(root, path),
                    f"{n} waivers for {cid}; time to look at the rule instead",
                )
            )
    return len(data), issues, warnings


def _lint_checks(
    root: Path,
    records: list[tuple[Path, dict[str, Any]]],
    discovered: list[tuple[Path, dict[str, Any]]],
) -> list[str]:
    issues: list[str] = []
    schema = load_schema("check-meta.schema.json")
    by_incident = {rec.get("id"): (p, rec) for p, rec in records}
    seen_ids: dict[str, Path] = {}
    git_non_exec = git_non_exec_checks(root)

    for program, meta in discovered:
        neither = "incident" not in meta and "origin" not in meta
        for issue in validate(meta, schema):
            if neither and issue.keyword == "anyOf":
                continue
            issues.append(_fmt(root, program, issue))
        if neither:
            issues.append(
                _err(
                    root,
                    program,
                    "has neither incident nor origin: a check needs a ledger record "
                    "(incident: F-...) or, for a reusable check, an origin",
                )
            )
        ident = meta.get("id")
        if isinstance(ident, str):
            if ident in seen_ids:
                issues.append(_err(root, program, f"id {ident} is also used by {rel_to(root, seen_ids[ident])}"))
            else:
                seen_ids[ident] = program
        exec_issue = check_executable_issue(root, program, git_non_exec=git_non_exec)
        if exec_issue:
            issues.append(_err(root, program, exec_issue))

        input_kind = meta.get("input") or "tree"
        incident = meta.get("incident")
        if incident:
            pair = by_incident.get(incident)
            if pair is None:
                issues.append(_err(root, program, f"incident {incident} has no record in .make-it-lint/ledger/"))
            else:
                rec_path, rec = pair
                rec_check = rec.get("check") if isinstance(rec.get("check"), dict) else {}
                rec_check_id = rec_check.get("id")
                if rec_check_id != ident:
                    issues.append(
                        _err(
                            root,
                            program,
                            f"id {ident!r} does not match check.id {rec_check_id!r} in {incident}",
                        )
                    )
                rec_path_val = rec_check.get("path")
                if rec_path_val:
                    target = root / rec_path_val
                    if target.exists():
                        try:
                            expected = resolve_check_program(target)
                        except CheckError:
                            expected = None
                        if expected is not None and expected.resolve() != program.resolve():
                            issues.append(
                                _err(root, program, f"record {incident} points at {rec_path_val}, not this file")
                            )
                if meta.get("layer") and rec.get("layer") and meta.get("layer") != rec.get("layer"):
                    issues.append(
                        _err(
                            root,
                            program,
                            f"metadata layer {meta.get('layer')!r} does not match record layer {rec.get('layer')!r}",
                        )
                    )
                if meta.get("tool") and rec_check.get("tool") and meta.get("tool") != rec_check.get("tool"):
                    issues.append(
                        _err(
                            root,
                            program,
                            f"metadata tool {meta.get('tool')!r} does not match record check.tool {rec_check.get('tool')!r}",
                        )
                    )
                proof = rec.get("proof") or {}
                rec_dir = rec_path.parent
                bad = rec_dir / as_rel(proof.get("bad") or "bad/")
                good = rec_dir / as_rel(proof.get("good") or "good/")
                issues.extend(_lint_input_fixtures(root, bad, good, input_kind))
            if meta.get("enforcement"):
                issues.append(
                    _err(
                        root,
                        program,
                        "has incident, so its state lives in the record; remove enforcement from the metadata",
                    )
                )
        else:
            if "enforcement" not in meta:
                issues.append(
                    _err(
                        root,
                        program,
                        "origin check without incident needs enforcement.state in metadata",
                    )
                )
            for child in check_entries(root):
                if child.is_dir() and program.is_relative_to(child):
                    bad, good = child / "bad", child / "good"
                    if bad.is_dir() and good.is_dir():
                        issues.extend(_lint_input_fixtures(root, bad, good, input_kind))
    return issues


def _lint_input_fixtures(root: Path, bad: Path, good: Path, input_kind: str) -> list[str]:
    issues: list[str] = []
    if input_kind == "tree":
        return issues
    if not bad.is_dir() or not good.is_dir():
        return issues
    multi = is_multi_fixture(bad, good)
    variants = [p for _, p in list_bad_variants(bad, multi=multi)] + [
        p for _, p in list_good_variants(good, multi=multi)
    ]
    for variant in variants:
        if not variant.is_dir():
            continue
        if input_kind == "diff":
            if not ((variant / "base").is_dir() and (variant / "head").is_dir()):
                issues.append(_err(root, variant, "a diff check needs base/ and head/ here"))
        elif input_kind == "action":
            if not (variant / "action.json").is_file():
                issues.append(_err(root, variant, "an action check needs action.json here"))
    return issues


def _lint_evidence(root: Path, path: Path, rec: dict[str, Any]) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    items = rec.get("evidence") or []
    if not isinstance(items, list) or not items:
        return issues, warnings
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        rel = item.get("path")
        if not rel:
            continue
        raw_path = Path(str(rel))
        if raw_path.is_absolute():
            issues.append(_err(root, path, f"evidence[{i}].path {rel} must be relative to the repository root"))
            continue
        target = root / raw_path
        if not is_within_root(root, target):
            issues.append(_err(root, path, f"evidence[{i}].path {rel} escapes the repository root"))
            continue
        if not target.is_file():
            issues.append(_err(root, path, f"evidence[{i}].path {rel} does not exist"))
            continue
        expected = item.get("sha256")
        if not expected:
            issues.append(_err(root, path, f"evidence[{i}].path {rel} needs sha256"))
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if str(expected).lower() != digest.lower():
            issues.append(_err(root, path, f"evidence[{i}].sha256 does not match {rel}"))
    return issues, warnings


def _lint_covers(root: Path, path: Path, rec: dict[str, Any]) -> list[str]:
    covers = rec.get("covers")
    if not isinstance(covers, dict):
        return []
    files = [str(f) for f in (covers.get("files") or []) if f]
    commit = covers.get("synced_commit")
    if not files or not commit:
        return []
    loc = rel_to(root, path)
    git = shutil.which("git")
    if not git:
        return [mil_line(loc, "git not available; skipped covers.files drift check")]
    probe = subprocess.run(
        [git, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if probe.returncode != 0:
        return [mil_line(loc, "git not available; skipped covers.files drift check")]
    logged = subprocess.run(
        [git, "-C", str(root), "log", f"{commit}..HEAD", "--oneline", "--", *files],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if logged.returncode != 0:
        return [mil_line(loc, f"git log {commit}..HEAD failed; skipped covers.files drift check")]
    if logged.stdout.strip():
        return [mil_line(loc, f"covers.files changed since {commit}")]
    return []


def _configured_kinds(root: Path) -> list[str] | None:
    path = config_path(root)
    if not path.is_file():
        return None
    try:
        data = yamlio.load_path(str(path))
    except yamlio.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    kinds = (data.get("ledger") or {}).get("kinds") if isinstance(data.get("ledger"), dict) else None
    if isinstance(kinds, list) and kinds:
        return [str(k) for k in kinds]
    return None


def _fmt(root: Path, path: Path, issue: SchemaIssue) -> str:
    loc = issue.path
    if loc.startswith("$"):
        loc = loc[1:]
        if loc.startswith("."):
            loc = loc[1:]
    where = rel_to(root, path)
    msg = issue.message
    if loc in ("", "$"):
        return mil_line(where, msg)
    return mil_line(where, loc, msg)


def _err(root: Path, path: Path, msg: str) -> str:
    return mil_line(rel_to(root, path), msg)
