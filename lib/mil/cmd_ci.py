"""Run product-tree checks according to ledger state.

shadow = print hits, do not fail. comment = GitHub annotations (and a
summary for a PR comment script). block = annotations and a non-zero
exit. mil ci does not use the network.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mil import yamlio
from mil.checks import CheckError, discover_checks, drop_ledger_fixture_hits, invoke_check_reproducible
from mil.errors import MilError, mil_line, rel_to
from mil.events import append_event, event_sinks
from mil.git_tree import current_branch, has_head, materialize_ref, materialize_staged
from mil.guard import audit_diff, hit_waived
from mil.ledger import iter_records, parse_date, today
from mil.models import RUNS_ON_PRODUCT, parse_hit_line
from mil.paths import config_path, is_ledger_fixture_path, mil_dir, waivers_path

REVIEW_MSG = "awaiting human review"

CI_EPILOG = """\
Run each check against the product tree, not the prove fixtures. State
comes from the ledger record when the check has incident, else from
metadata enforcement.state (origin checks).

  shadow   print hits; the job stays green
  comment  GitHub annotations (::warning); the job stays green
  block    GitHub annotations (::error); hits fail the job

States before shadow (open, proven), plus retired, are skipped.
human_review records print as "awaiting human review" and stay visible.
A matching unexpired waiver downgrades block to comment for that path.
Action checks (`input: action`) run in `mil guard` (PreToolUse hook) and
are skipped here. Diff checks need --base/--head, --base-ref, or --staged.

--staged materializes HEAD and the git index into clean temporary trees
so untracked working-tree files cannot pollute checks. --base-ref and
--head-ref do the same for git refs in CI without git worktrees or tar.

git usage:
  mil ci --staged
  mil ci --base-ref origin/main --head-ref HEAD --json-summary mil-summary.json

examples:
  mil ci
  mil ci --events mil-events.ndjson
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "ci",
        help="run product checks by state: shadow reports, comment annotates, block fails",
        description="Run product checks by state: shadow reports, comment annotates, block fails.",
        epilog=CI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("--base", metavar="DIR", help="base tree for input: diff checks")
    p.add_argument("--head", metavar="DIR", help="head tree for input: diff checks (default: --root)")
    p.add_argument("--base-ref", metavar="REF", help="materialize git REF as base tree")
    p.add_argument("--head-ref", metavar="REF", help="materialize git REF as clean head/product tree")
    p.add_argument("--staged", action="store_true", help="check staged git index against HEAD")
    p.add_argument(
        "--json-summary",
        metavar="PATH",
        help="write a JSON summary (for a PR comment script; mil does not post it)",
    )
    p.add_argument(
        "--events",
        metavar="FILE",
        help="append hit/waiver/shadow_run events as JSON lines (never under ledger/)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    repo_root: Path = args.root
    if args.staged and (args.base or args.head or args.base_ref or args.head_ref):
        raise MilError(mil_line("ci", "--staged cannot be combined with --base/--head or --base-ref/--head-ref"))
    if args.base_ref and args.base:
        raise MilError(mil_line("ci", "use either --base or --base-ref, not both"))
    if args.head_ref and args.head:
        raise MilError(mil_line("ci", "use either --head or --head-ref, not both"))

    if args.staged or args.base_ref or args.head_ref:
        with tempfile.TemporaryDirectory(prefix="mil-ci-") as tmp:
            tmp_dir = Path(tmp)
            base_dir: Path | None = None
            head_dir: Path | None = None
            target_root = repo_root
            prev_branch = os.environ.get("MIL_PR_BRANCH")
            try:
                if args.staged:
                    if not prev_branch:
                        br = current_branch(repo_root)
                        if br:
                            os.environ["MIL_PR_BRANCH"] = br
                    head_dir = tmp_dir / "head"
                    materialize_staged(repo_root, head_dir)
                    target_root = head_dir
                    if has_head(repo_root):
                        base_dir = tmp_dir / "base"
                        materialize_ref(repo_root, "HEAD", base_dir)
                else:
                    if args.base_ref:
                        base_dir = tmp_dir / "base"
                        materialize_ref(repo_root, args.base_ref, base_dir)
                    if args.head_ref:
                        head_dir = tmp_dir / "head"
                        materialize_ref(repo_root, args.head_ref, head_dir)
                        target_root = head_dir
                    elif base_dir is not None:
                        head_dir = repo_root
                return _execute_ci(
                    repo_root=repo_root,
                    target_root=target_root,
                    base=base_dir,
                    head=head_dir,
                    json_summary=args.json_summary,
                    events_arg=getattr(args, "events", None),
                )
            finally:
                if prev_branch is None:
                    os.environ.pop("MIL_PR_BRANCH", None)
                else:
                    os.environ["MIL_PR_BRANCH"] = prev_branch

    base = Path(args.base).resolve() if args.base else None
    head = Path(args.head).resolve() if args.head else (repo_root if args.base else None)
    if args.base and (base is None or not base.is_dir()):
        raise MilError(mil_line("ci", f"--base {args.base}: not a directory"))
    if args.head and (head is None or not head.is_dir()):
        raise MilError(mil_line("ci", f"--head {args.head}: not a directory"))
    return _execute_ci(
        repo_root=repo_root,
        target_root=repo_root,
        base=base,
        head=head,
        json_summary=args.json_summary,
        events_arg=getattr(args, "events", None),
    )


def _execute_ci(
    *,
    repo_root: Path,
    target_root: Path,
    base: Path | None,
    head: Path | None,
    json_summary: str | None,
    events_arg: str | None,
) -> int:
    if not mil_dir(target_root).is_dir():
        raise MilError(mil_line(target_root, "no .make-it-lint directory"))
    config = config_path(target_root)
    config_data = _load_config(config)
    github = os.environ.get("GITHUB_ACTIONS") == "true"

    records = {rec.get("id"): rec for _, rec in iter_records(target_root)}
    found, errors = discover_checks(target_root)
    waivers = _load_waivers(target_root)
    jobs: list[dict[str, Any]] = []
    sinks = event_sinks(repo_root, events_arg, config_data)

    for _, rec in iter_records(target_root):
        if (rec.get("enforcement") or {}).get("state") == "human_review":
            if (rec.get("check") or {}).get("path"):
                continue
            jobs.append(_review_job(rec))

    for child, exc in errors:
        jobs.append(
            {
                "id": child.name,
                "incident": None,
                "state": "block",
                "effective_state": "block",
                "result": "error",
                "detail": str(exc),
                "hits": [],
                "skipped": False,
                "evidence": [],
                "seed": 0,
            }
        )

    for program, meta in found:
        jobs.append(
            _job_for_check(
                target_root,
                program,
                meta,
                records,
                base,
                head,
                config,
                waivers,
                config_data=config_data,
            )
        )

    protected_changes: list[dict[str, Any]] = []
    if base is not None and head is not None:
        protected_changes = audit_diff(base, head, root=repo_root, config_data=config_data)

    block_fail = 0
    shadow_hits = 0
    comment_hits = 0
    check_errors = 0
    ran = 0
    skipped = 0
    reviews = 0
    for job in jobs:
        _print_job(job, github=github)
        if job.get("human_review"):
            reviews += 1
            continue
        if job.get("skipped"):
            skipped += 1
            continue
        ran += 1
        _emit_ci_events(sinks, repo_root, job)
        if job.get("result") == "error":
            check_errors += 1
            if (job.get("effective_state") or job.get("state")) == "block":
                block_fail += 1
            continue
        for hit in job.get("hits") or []:
            hit_state = hit.get("state")
            if hit_state == "block":
                block_fail += 1
            elif hit_state == "comment":
                comment_hits += 1
            elif hit_state == "shadow":
                shadow_hits += 1

    for item in protected_changes:
        if item["severity"] == "block":
            block_fail += 1
            print(f"ERR   MIL-GUARD - block: {item['path']}: {item['detail']}")
            if github:
                print(_workflow_cmd("error", "MIL-GUARD", item["path"], None, item["detail"]))
        else:
            print(f"guard {item['kind']} {item['path']}: {item['detail']}")
            if github:
                print(_workflow_cmd("notice", "MIL-GUARD", item["path"], None, item["detail"]))

    extra = ""
    if reviews:
        extra = f", {reviews} awaiting human review"
    print(
        f"mil ci: {ran} ran, {skipped} skipped, "
        f"{shadow_hits} shadow hit(s), {comment_hits} comment hit(s), {block_fail} block hit(s)"
        f"{extra}"
    )
    if json_summary:
        payload = {
            "ran": [j for j in jobs if not j.get("skipped") and not j.get("human_review")],
            "skipped": [j for j in jobs if j.get("skipped") or j.get("human_review")],
            "protected_changes": protected_changes,
            "shadow_hits": shadow_hits,
            "comment_hits": comment_hits,
            "block_hits": block_fail,
            "check_errors": check_errors,
            "awaiting_human_review": reviews,
        }
        Path(json_summary).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 1 if block_fail else 0


def _review_job(rec: dict[str, Any]) -> dict[str, Any]:
    ident = str(rec.get("id") or "")
    return {
        "id": ident,
        "incident": ident,
        "state": "human_review",
        "effective_state": "human_review",
        "result": "review",
        "hits": [],
        "skipped": True,
        "human_review": True,
        "reason": REVIEW_MSG,
        "detail": None,
        "evidence": _evidence_refs(rec),
        "seed": _seed_of(rec, {}),
        "files": [],
    }


def _job_for_check(
    root: Path,
    program: Path,
    meta: dict[str, Any],
    records: dict[Any, dict[str, Any]],
    base: Path | None,
    head: Path | None,
    config: Path,
    waivers: list[dict[str, Any]],
    *,
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ident = str(meta.get("id") or program.name)
    incident = meta.get("incident")
    rec = records.get(incident) if incident else None
    if incident and rec is None:
        return {
            "id": ident,
            "incident": incident,
            "path": rel_to(root, program),
            "state": "block",
            "effective_state": "block",
            "result": "error",
            "hits": [],
            "skipped": False,
            "reason": None,
            "detail": f"incident {incident} has no record in .make-it-lint/ledger/",
            "evidence": [],
            "seed": _seed_of(None, meta),
            "files": [],
        }

    if rec is not None:
        state = str((rec.get("enforcement") or {}).get("state") or "")
    else:
        state = str(((meta.get("enforcement") or {}).get("state")) or "shadow")
    job: dict[str, Any] = {
        "id": ident,
        "incident": incident,
        "path": rel_to(root, program),
        "state": state,
        "effective_state": state,
        "result": "skip",
        "hits": [],
        "skipped": True,
        "reason": None,
        "detail": None,
        "evidence": _evidence_refs(rec),
        "seed": _seed_of(rec, meta),
        "files": [],
    }
    if state == "human_review":
        job["reason"] = REVIEW_MSG
        job["human_review"] = True
        return job
    if state not in RUNS_ON_PRODUCT:
        job["reason"] = f"state {state or 'unset'} is not run against product code"
        return job
    kind = meta.get("input") or "tree"
    if kind == "action":
        job["reason"] = "action check runs in mil guard (PreToolUse hook)"
        return job
    if kind == "diff" and (base is None or head is None):
        job["reason"] = "diff check needs --base and --head (or --base-ref / --staged)"
        return job
    layer = None
    if rec is not None and rec.get("layer"):
        layer = str(rec.get("layer"))
    elif meta.get("layer"):
        layer = str(meta.get("layer"))
    seed = job["seed"]
    try:
        if kind == "diff":
            proc = invoke_check_reproducible(
                program,
                meta,
                layer=layer,
                seed=seed,
                root=root,
                config=config,
                base_dir=base,
                head_dir=head,
                config_data=config_data,
            )
        else:
            proc = invoke_check_reproducible(
                program,
                meta,
                layer=layer,
                seed=seed,
                root=root,
                config=config,
                config_data=config_data,
            )
    except CheckError as exc:
        job["skipped"] = False
        job["result"] = "error"
        job["detail"] = str(exc)
        return job
    except Exception as exc:  # noqa: BLE001 — surface a broken check as ci output, not a traceback
        job["skipped"] = False
        job["result"] = "error"
        job["detail"] = str(exc)
        return job

    job["skipped"] = False
    rc = proc.returncode
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if rc == 2 or rc not in (0, 1):
        job["result"] = "error"
        job["detail"] = (stderr or stdout or f"exit {rc}").strip()
        return job

    hits = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parsed = parse_hit_line(line)
        if parsed is None:
            if rc == 1:
                job["result"] = "error"
                job["detail"] = f"stdout not in '<path>[:<line>]: <message>' form: {line!r}"
                return job
            continue
        path, lineno, message = parsed
        if is_ledger_fixture_path(path):
            continue
        effective = state
        waived = False
        if state == "block" and hit_waived(waivers, ident, path):
            effective = "comment"
            waived = True
        hits.append(
            {
                "path": path,
                "line": lineno,
                "message": message,
                "state": effective,
                "waived": waived,
            }
        )
    hits = drop_ledger_fixture_hits(hits)
    job["hits"] = hits
    job["files"] = [h["path"] for h in hits]
    if hits:
        job["result"] = "hit"
        if any(h["state"] == "block" for h in hits):
            job["effective_state"] = "block"
        elif any(h["state"] == "comment" for h in hits):
            job["effective_state"] = "comment"
        else:
            job["effective_state"] = "shadow"
    else:
        job["result"] = "no-hit"
        job["effective_state"] = state
    return job


def _print_job(job: dict[str, Any], *, github: bool) -> None:
    ident = job.get("id") or ""
    incident = job.get("incident") or "-"
    state = job.get("effective_state") or job.get("state") or "-"
    if job.get("human_review") or job.get("reason") == REVIEW_MSG:
        label = incident if str(incident).startswith("F-") else ident
        if not label or label == "-":
            label = ident
        print(f"review {label}: {REVIEW_MSG}")
        return
    if job.get("skipped"):
        print(f"skip  {ident} {incident}: {job.get('reason')}")
        return
    result = job.get("result")
    extra = _evidence_seed_suffix(job)
    if result == "error":
        print(f"ERR   {ident} {incident} {state}: {job.get('detail')}{extra}")
        if github and state == "block":
            print(_workflow_cmd("error", ident, None, None, str(job.get("detail") or "check error")))
        elif github:
            print(_workflow_cmd("warning", ident, None, None, str(job.get("detail") or "check error")))
        return
    n = len(job.get("hits") or [])
    print(f"{result:<5} {ident} {incident} {state}: {n} hit(s){extra}")
    for hit in job.get("hits") or []:
        loc = hit["path"] + (f":{hit['line']}" if hit.get("line") else "")
        print(f"      {loc}: {hit['message']}")
        if github:
            kind = "error" if hit.get("state") == "block" else "warning" if hit.get("state") == "comment" else "notice"
            print(_workflow_cmd(kind, ident, hit.get("path"), hit.get("line"), hit.get("message") or ""))


def _evidence_seed_suffix(job: dict[str, Any]) -> str:
    parts = []
    shown, rest = _trim_evidence(job.get("evidence") or [])
    if shown:
        text = ", ".join(shown)
        if rest:
            text += " (+%d)" % rest
        parts.append("evidence: " + text)
    seed = job.get("seed")
    if seed is not None:
        parts.append("seed: %s" % seed)
    if not parts:
        return ""
    return "  " + "  ".join(parts)


def _trim_evidence(refs: list[str]) -> tuple[list[str], int]:
    if len(refs) <= 3:
        return refs, 0
    return refs[:3], len(refs) - 3


def _evidence_refs(rec: dict[str, Any] | None) -> list[str]:
    if not rec:
        return []
    refs = []
    for item in rec.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        if item.get("path"):
            refs.append(str(item.get("path")))
        elif item.get("url"):
            refs.append(str(item.get("url")))
    return refs


def _seed_of(rec: dict[str, Any] | None, meta: dict[str, Any]) -> int:
    if rec is not None:
        proof = rec.get("proof") or {}
        if isinstance(proof, dict) and proof.get("seed") is not None:
            try:
                return int(proof["seed"])
            except (TypeError, ValueError):
                return 0
    if meta.get("seed") is not None:
        try:
            return int(meta["seed"])
        except (TypeError, ValueError):
            return 0
    return 0


def _workflow_cmd(kind: str, check_id: str, path: str | None, line: int | None, message: str) -> str:
    text = f"{check_id}: {message}"
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    extra = []
    if path:
        extra.append(f"file={path}")
    if line:
        extra.append(f"line={line}")
    extra.append(f"title={check_id}")
    return f"::{kind} {','.join(extra)}::{text}"


def _load_waivers(root: Path) -> list[dict[str, Any]]:
    path = waivers_path(root)
    if not path.is_file():
        return []
    try:
        data = yamlio.load_path(str(path))
    except yamlio.YAMLError:
        return []
    if not isinstance(data, list):
        return []
    now = parse_date(today())
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        expires = parse_date(item.get("expires"))
        if now is not None and expires is not None and expires < now:
            continue
        out.append(item)
    return out


def _load_config(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = yamlio.load_path(str(path))
    except yamlio.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _emit_ci_events(sinks: list[Path], root: Path, job: dict[str, Any]) -> None:
    if not sinks:
        return
    ident = str(job.get("id") or "")
    incident = str(job.get("incident") or "")
    raw_state = str(job.get("state") or "")
    state = str(job.get("effective_state") or raw_state)
    if job.get("result") == "error":
        for sink in sinks:
            append_event(
                sink,
                {
                    "type": "check_error",
                    "check": ident,
                    "incident": incident,
                    "path": "",
                    "detail": str(job.get("detail") or "error"),
                },
                root=root,
            )
        return
    if raw_state == "shadow":
        for sink in sinks:
            append_event(
                sink,
                {
                    "type": "shadow_run",
                    "check": ident,
                    "incident": incident,
                    "path": "",
                    "detail": "ran",
                },
                root=root,
            )
    for hit in job.get("hits") or []:
        hit_state = hit.get("state") or state
        etype = "shadow_hit"
        if hit_state == "comment":
            etype = "comment_hit"
        elif hit_state == "block":
            etype = "block_hit"
        for sink in sinks:
            append_event(
                sink,
                {
                    "type": etype,
                    "check": ident,
                    "incident": incident,
                    "path": hit.get("path") or "",
                    "detail": hit.get("message") or "",
                },
                root=root,
            )
        if hit.get("waived"):
            for sink in sinks:
                append_event(
                    sink,
                    {
                        "type": "waiver",
                        "check": ident,
                        "incident": incident,
                        "path": hit.get("path") or "",
                        "detail": "block downgraded to comment",
                    },
                    root=root,
                )
