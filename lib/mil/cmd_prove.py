"""Run checks against bad/ and good/ fixtures.

A proof passes only when the check exits 1 with at least one
`<path>[:<line>]: <message>` line on every bad fixture, and exits 0 on
every good fixture.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mil import yamlio
from mil.checks import (
    CheckError,
    NonReproducibleError,
    check_entries,
    check_executable_issue,
    extract_metadata,
    git_non_exec_checks,
    invoke_check_reproducible,
    resolve_check_program,
)
from mil.errors import MilError, mil_line
from mil.events import append_event, event_sinks
from mil.fixtures import (
    is_multi_fixture,
    is_multi_side,
    list_bad_variants,
    list_good_variants,
    stray_multi_files,
)
from mil.hunks import hunk_warnings_for
from mil.ledger import find_by_id, iter_records, save_record_file, today
from mil.models import HIT_RE, PROVEN_OR_LATER, PROVEN_RESULT, RECORD_ID_RE
from mil.paths import as_rel, config_path, contract_doc_rel, mil_dir

REVIEW_MSG = "awaiting human review"

PROVE_EPILOG = f"""\
Each check is called as in {contract_doc_rel()}:

  tree checks:    <check> --root <fixture> --config .make-it-lint/config.yaml
  diff checks:    <check> --base <fixture>/base --head <fixture>/head --config ...
  action checks:  <check> --action <fixture>/action.json --root <fixture> --config ...

Check-specific config is also passed as JSON in MIL_CHECK_CONFIG_JSON.
If the fixture has pr.yaml (branch, labels, kind), MIL_PR_BRANCH,
MIL_PR_LABELS, and MIL_PR_KIND are set from it; outer shell values are
never leaked into fixture runs. MIL_SEED comes from proof.seed (or 0).
layer: verify checks run twice and exit 2 if exit code or stdout differs.

On a real success, if the record is in state=open, mil sets
enforcement.state to proven and appends history. shadow / comment / block
are not moved. --dry-run skips writing record.yaml.

--check-hunks reverts one fix hunk at a time from good/ back toward bad/
(skipping comment/docstring-only hunks) and warns when good/ stays green.

examples:
  mil prove --all --dry-run
  mil prove --all --check-hunks --dry-run
"""


@dataclass(frozen=True)
class _SideOutcome:
    verdict: str
    stdout: str
    stderr: str
    reason: str
    non_reproducible: bool = False


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "prove",
        help="prove checks: red (exit 1) on bad/, green (exit 0) on good/",
        description="Prove checks: red (exit 1) on bad/, green (exit 0) on good/.",
        epilog=PROVE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("ids", nargs="*", metavar="F-YYYYMMDD-NN")
    p.add_argument(
        "--all",
        action="store_true",
        help="every record with a check, plus generic checks with bad/ and good/",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="do not write proof.last_result or enforcement.state",
    )
    p.add_argument(
        "--verify-recorded",
        action="store_true",
        help="fail if a proven/shadow/comment/block record's stored last_result disagrees with the live run",
    )
    p.add_argument(
        "--check-hunks",
        action="store_true",
        help="revert fix hunks onto good/ and warn when the check stays green",
    )
    p.add_argument(
        "--events",
        metavar="FILE",
        help="append prove events as JSON lines (never under ledger/)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    if not mil_dir(root).is_dir():
        raise MilError(mil_line(root, "no .make-it-lint directory"))
    ids = list(args.ids or [])
    if args.all and ids:
        raise MilError(mil_line("prove", "use either --all or an id, not both"))
    if not args.all and not ids:
        raise MilError(mil_line("prove", "give one or more record IDs (F-YYYYMMDD-NN), or --all"))

    targets: list[dict[str, Any]] = []
    if args.all:
        for rec_path, rec in iter_records(root):
            state = (rec.get("enforcement") or {}).get("state")
            if state == "human_review":
                targets.append(_review_job(rec_path, rec))
                continue
            if not (rec.get("check") or {}).get("path"):
                continue
            targets.append(_target_from_record(root, rec_path, rec))
        for job in _generic_fixture_jobs(root):
            targets.append(job)
    else:
        for rid in ids:
            if not RECORD_ID_RE.match(rid):
                raise MilError(mil_line("prove", f"{rid!r} is not a record ID (F-YYYYMMDD-NN)"))
            found = find_by_id(root, rid)
            if not found:
                raise MilError(mil_line("prove", f"{rid}: not found"))
            rec_path, rec = found
            if (rec.get("enforcement") or {}).get("state") == "human_review":
                targets.append(_review_job(rec_path, rec))
            else:
                targets.append(_target_from_record(root, rec_path, rec))

    if not targets:
        print("nothing to prove: no record has a check, and no check has bad/ and good/")
        return 0

    config_data = _load_config(root)
    git_non_exec = git_non_exec_checks(root)
    if args.dry_run:
        sinks = event_sinks(root, getattr(args, "events", None), None)
    else:
        sinks = event_sinks(root, getattr(args, "events", None), config_data)

    failed = 0
    broken = 0
    count = 0
    for job in targets:
        if job.get("human_review"):
            print("review %s: %s" % (job["label"], REVIEW_MSG))
            continue
        count += 1
        if job.get("error") or job.get("program") is None:
            err = job.get("error") or "nothing to prove"
            print(f"FAIL {job['label']}: {err}")
            failed += 1
            continue
        program = job["program"]
        metadata = job["metadata"]
        bad, good = job["bad"], job["good"]
        rec = job.get("rec")
        seed = _seed_for(rec, metadata)
        layer = _layer_for(rec, metadata)
        ok, result, detail, non_repro = _prove_one(
            root,
            program,
            metadata,
            bad,
            good,
            seed=seed,
            layer=layer,
            config_data=config_data,
            git_non_exec=git_non_exec,
        )
        if ok and args.check_hunks:
            for warn in hunk_warnings_for(
                program, metadata, root, config_path(root), bad, good, seed=seed, layer=layer
            ):
                detail.append("warn: " + warn)
        if ok and args.verify_recorded and isinstance(rec, dict):
            state = (rec.get("enforcement") or {}).get("state")
            recorded = (rec.get("proof") or {}).get("last_result")
            if state in PROVEN_OR_LATER and recorded != result:
                ok = False
                detail.append(f"recorded proof.last_result {recorded!r} != live {result!r}")
        check_id = metadata.get("id") or ""
        print(f"{'ok  ' if ok else 'FAIL'} {job['label']} {check_id}: {result}".rstrip())
        for line in detail:
            print("     " + line)
        if not ok:
            failed += 1
            if non_repro:
                broken += 1
        if not args.dry_run and rec is not None and job.get("rec_path") is not None:
            _write_proof(job["rec_path"], rec, result, ok)
        if sinks:
            _emit_prove(sinks, root, job, result, ok)
    print(f"mil prove: {count - failed} of {count} proved")
    if broken:
        return 2
    return 1 if failed else 0


def _review_job(rec_path: Path, rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": str(rec.get("id") or rec_path.parent.name),
        "rec_path": rec_path,
        "rec": rec,
        "human_review": True,
    }


def _target_from_record(root: Path, rec_path: Path, rec: dict[str, Any]) -> dict[str, Any]:
    label = str(rec.get("id") or rec_path.parent.name)
    job = _job_from_record(root, rec_path, rec)
    if isinstance(job, str):
        return {
            "label": label,
            "rec_path": rec_path,
            "rec": rec,
            "error": job,
            "metadata": {"error": job, "id": ""},
        }
    return job


def _write_proof(rec_path: Path, rec: dict[str, Any], result: str, ok: bool) -> None:
    rec.setdefault("proof", {})
    if rec["proof"] is None:
        rec["proof"] = {}
    changed = False
    if rec["proof"].get("last_result") != result:
        rec["proof"]["last_result"] = result
        changed = True
    if ok and _advance_to_proven(rec):
        changed = True
    if changed:
        save_record_file(rec_path, rec)


def _advance_to_proven(rec: dict[str, Any]) -> bool:
    """Move open -> proven on a passing proof."""
    current = (rec.get("enforcement") or {}).get("state")
    if current != "open":
        return False
    at = today()
    hist = rec.setdefault("history", [])
    if not isinstance(hist, list):
        hist = []
        rec["history"] = hist
    hist.append({"state": "proven", "at": at, "note": "mil prove"})
    rec.setdefault("enforcement", {})["state"] = "proven"
    return True


def _job_from_record(
    root: Path, rec_path: Path, rec: dict[str, Any]
) -> dict[str, Any] | str:
    check = rec.get("check") or {}
    rel = check.get("path")
    if not rel:
        return f"{rec.get('id')}: has no check.path; nothing to prove"
    program_path = (root / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
    try:
        program = resolve_check_program(program_path)
        metadata = extract_metadata(program)
    except (CheckError, yamlio.YAMLError) as exc:
        return str(exc)
    proof = rec.get("proof") or {}
    rec_dir = rec_path.parent
    bad = rec_dir / as_rel(proof.get("bad") or "bad/")
    good = rec_dir / as_rel(proof.get("good") or "good/")
    for p in (bad, good):
        if not p.is_dir():
            return f"{rec.get('id')}: {p.name}/ is missing"
    multi = is_multi_fixture(bad, good)
    if multi:
        if is_multi_side(bad) and not (bad / "real").is_dir():
            return f"{rec.get('id')}: bad/real/ is required when using several bad trees"
        if stray_multi_files(bad):
            return f"{rec.get('id')}: unexpected top-level files in multi-variant bad/"
        if stray_multi_files(good):
            return f"{rec.get('id')}: unexpected top-level files in multi-variant good/"
        if not list_bad_variants(bad, multi=True):
            return f"{rec.get('id')}: bad/<name>/ is required in multi-variant layout"
        if not list_good_variants(good, multi=True):
            return f"{rec.get('id')}: good/<name>/ is required when using multi-variant fixtures"
    return {
        "label": str(rec.get("id")),
        "rec_path": rec_path,
        "rec": rec,
        "program": program,
        "metadata": metadata,
        "bad": bad,
        "good": good,
    }


def _generic_fixture_jobs(root: Path) -> list[dict[str, Any]]:
    jobs = []
    for child in check_entries(root):
        if not child.is_dir():
            continue
        bad, good = child / "bad", child / "good"
        if not bad.is_dir() or not good.is_dir():
            continue
        try:
            program = resolve_check_program(child)
            metadata = extract_metadata(program)
        except (CheckError, yamlio.YAMLError) as exc:
            raise MilError(mil_line("prove", str(exc))) from exc
        if metadata.get("incident"):
            continue
        label = str(metadata.get("id") or child.name)
        jobs.append(
            {
                "label": label,
                "rec_path": None,
                "rec": None,
                "program": program,
                "metadata": metadata,
                "bad": bad,
                "good": good,
            }
        )
    return jobs


def _seed_for(rec: dict[str, Any] | None, metadata: dict[str, Any]) -> int:
    if rec is not None:
        proof = rec.get("proof") or {}
        if isinstance(proof, dict) and proof.get("seed") is not None:
            try:
                return int(proof["seed"])
            except (TypeError, ValueError):
                return 0
    if metadata.get("seed") is not None:
        try:
            return int(metadata["seed"])
        except (TypeError, ValueError):
            return 0
    return 0


def _layer_for(rec: dict[str, Any] | None, metadata: dict[str, Any]) -> str | None:
    if rec is not None and rec.get("layer"):
        return str(rec.get("layer"))
    if metadata.get("layer"):
        return str(metadata.get("layer"))
    return None


def _load_config(root: Path) -> dict[str, Any] | None:
    path = config_path(root)
    if not path.is_file():
        return None
    try:
        data = yamlio.load_path(str(path))
    except yamlio.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _prove_one(
    root: Path,
    program: Path,
    metadata: dict[str, Any],
    bad: Path,
    good: Path,
    *,
    seed: int,
    layer: str | None,
    config_data: dict[str, Any] | None = None,
    git_non_exec: set[Path] | None = None,
) -> tuple[bool, str, list[str], bool]:
    detail: list[str] = []
    exec_issue = check_executable_issue(root, program, git_non_exec=git_non_exec)
    if exec_issue:
        detail.append(f"bad: {program} {exec_issue}")
        return False, "error-on-bad, error-on-good", detail, False
    config = config_path(root)
    multi = is_multi_fixture(bad, good)
    bads = list_bad_variants(bad, multi=multi)
    goods = list_good_variants(good, multi=multi)
    if multi and not bads:
        return False, "error-on-bad, error-on-good", ["bad/<name>/ is missing"], False
    if multi and not goods:
        return False, "error-on-bad, error-on-good", ["good/<name>/ is missing"], False

    red_bad = 0
    green_good = 0
    pair_bad_label = None
    pair_good_label = None
    snippets: list[tuple[str, str, str]] = []
    non_repro = False

    for label, directory in bads:
        outcome = _run_side(
            program,
            metadata,
            root,
            config,
            directory,
            seed=seed,
            layer=layer,
            expect="bad",
            config_data=config_data,
        )
        snippets.append((label, outcome.stdout, outcome.stderr))
        if outcome.non_reproducible:
            non_repro = True
        if outcome.verdict == "red-on-bad":
            red_bad += 1
        elif outcome.reason:
            detail.append(f"{label}: {outcome.reason}")
        if not multi:
            pair_bad_label = outcome.verdict

    for label, directory in goods:
        outcome = _run_side(
            program,
            metadata,
            root,
            config,
            directory,
            seed=seed,
            layer=layer,
            expect="good",
            config_data=config_data,
        )
        snippets.append((label, outcome.stdout, outcome.stderr))
        if outcome.non_reproducible:
            non_repro = True
        if outcome.verdict == "green-on-good":
            green_good += 1
        elif outcome.reason:
            detail.append(f"{label}: {outcome.reason}")
        if not multi:
            pair_good_label = outcome.verdict

    n_bad, n_good = len(bads), len(goods)
    if multi:
        result = "red-on-bad %d/%d, green-on-good %d/%d" % (red_bad, n_bad, green_good, n_good)
        ok = red_bad == n_bad > 0 and green_good == n_good > 0
    else:
        result = "%s, %s" % (pair_bad_label or "error-on-bad", pair_good_label or "error-on-good")
        ok = result == PROVEN_RESULT
    if detail:
        for name, out, err in snippets[:4]:
            for ln in (out.splitlines() + err.splitlines())[:10]:
                detail.append("  %s| %s" % (name, ln))
    return ok, result, detail, non_repro


def _run_side(
    program: Path,
    metadata: dict[str, Any],
    root: Path,
    config: Path,
    fixture_dir: Path,
    *,
    seed: int,
    layer: str | None,
    expect: str,
    config_data: dict[str, Any] | None = None,
) -> _SideOutcome:
    err_verdict = "error-on-bad" if expect == "bad" else "error-on-good"
    try:
        proc = invoke_check_reproducible(
            program,
            metadata,
            root=root,
            config=config,
            fixture_dir=fixture_dir,
            seed=seed,
            layer=layer,
            config_data=config_data,
        )
    except NonReproducibleError as exc:
        return _SideOutcome(err_verdict, "", "", str(exc), non_reproducible=True)
    except CheckError as exc:
        return _SideOutcome(err_verdict, "", "", str(exc))
    except Exception as exc:  # noqa: BLE001 — surface fixture/runner crashes as prove failures
        return _SideOutcome(err_verdict, "", "", str(exc))

    rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    if expect == "bad":
        hits = [ln for ln in out.splitlines() if ln.strip()]
        if rc == 1:
            bad_lines = [ln for ln in hits if not HIT_RE.match(ln)]
            if not hits:
                return _SideOutcome("error-on-bad", out, err, "exit 1 but no hit printed on stdout")
            if bad_lines:
                return _SideOutcome(
                    "error-on-bad",
                    out,
                    err,
                    f"stdout lines not in '<path>[:<line>]: <message>' form: {bad_lines[0]!r}",
                )
            return _SideOutcome("red-on-bad", out, err, "")
        if rc == 0:
            return _SideOutcome("green-on-bad", out, err, "exit 0, the check did not catch the failure")
        msg = f"exit {rc} (2 means the check is broken)" if rc is not None else err
        return _SideOutcome("error-on-bad", out, err, msg)

    if rc == 0:
        return _SideOutcome("green-on-good", out, err, "")
    if rc == 1:
        return _SideOutcome("red-on-good", out, err, "exit 1, the check flags the fixed version")
    msg = f"exit {rc} (2 means the check is broken)" if rc is not None else err
    return _SideOutcome("error-on-good", out, err, msg)


def _emit_prove(
    sinks: list[Path],
    root: Path,
    job: dict[str, Any],
    result: str,
    ok: bool,
) -> None:
    metadata = job.get("metadata") or {}
    rec = job.get("rec") or {}
    event = {
        "type": "prove",
        "check": metadata.get("id") or "",
        "incident": rec.get("id") or metadata.get("incident") or "",
        "path": "",
        "detail": result if ok else "%s fail" % result,
    }
    for sink in sinks:
        append_event(sink, event, root=root)
