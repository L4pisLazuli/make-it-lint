"""Move a proven record one step toward block, after a human OK at a terminal.

mil never promotes on its own. `--approved-by` names who said yes and is
stored in history; it is not permission to write. A real promotion
refuses known agent runtimes, requires stdin to be a terminal, and asks
the person to type the exact target state (`shadow`, `comment`, or `block`).
Before promoting to `block`, mil also checks that the rule is currently
green on the product tree so the next commit and CI are not immediately
broken.
"""

from __future__ import annotations

import argparse
import copy
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from mil import yamlio
from mil.checks import discover_checks
from mil.errors import MilError, mil_line
from mil.git_tree import current_branch, has_head, materialize_ref, materialize_staged
from mil.human_ok import approver_looks_like_agent, confirm_promote, history_entry
from mil.ledger import find_by_id, save_record_file, today
from mil.models import ROLLOUT, is_proven_result
from mil.paths import config_path, mil_dir

PROMOTABLE = ("proven", "shadow", "comment")
TARGETS = ("shadow", "comment", "block")

PROMOTE_EPILOG = """\
A person runs this in their own terminal after saying yes to this exact
step. Agents must not run it: they ask the user, then print the command.
`--approved-by` is the name stored in history, not permission to write.

  proven  -> shadow   report hits in CI/hooks, job stays green, no PR comment
  shadow  -> comment  annotations and a PR comment on hits, job stays green
  comment -> block    annotations, a PR comment, and a failed job/hook

Before a step to `block`, mil runs the check against the current product
tree (and for `input: diff`, compares git HEAD against the staged index)
and refuses when it hits or errors (`--dry-run` runs the same check).

The write is refused when a known agent marker (`CURSOR_AGENT`,
`CLAUDECODE`, `CLAUDE_CODE`, `CODEX_SANDBOX`, `AIDER`) is set, when stdin
is not a terminal, or when the typed confirmation line does not match the
next state. `--dry-run` skips the TTY gate and writes nothing.

usage at a terminal, after a yes:
  mil promote F-20260311-01 --approved-by @owner
  mil promote F-20260311-01 --to comment --approved-by @owner --note "10 shadow runs, 0 false positives"
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "promote",
        help="move a proven record one step toward block, after a human OK at a terminal",
        description="Move a record proven -> shadow -> comment -> block, one step per call.",
        epilog=PROMOTE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument("incident", metavar="F-YYYYMMDD-NN", help="ledger record id")
    p.add_argument(
        "--to",
        choices=TARGETS,
        help="the state to move to; must be the next one (default: the next one)",
    )
    p.add_argument(
        "--approved-by",
        required=True,
        metavar="WHO",
        help="who said yes to this step, e.g. @name; stored in history, not a gate",
    )
    p.add_argument("--note", help="why now, e.g. '10 shadow runs, 0 false positives'")
    p.add_argument("--dry-run", action="store_true", help="check preconditions, print the step, and write nothing")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    if not mil_dir(root).is_dir():
        raise MilError(mil_line(root, "no .make-it-lint directory"))
    found = find_by_id(root, args.incident)
    if found is None:
        raise MilError(mil_line(args.incident, "no such ledger record"))
    rec_path, rec = found
    approved_by = args.approved_by.strip()
    if not approved_by:
        raise MilError(mil_line(rec_path, "--approved-by is empty", "name who said yes"))
    if approver_looks_like_agent(approved_by):
        raise MilError(
            mil_line(
                rec_path,
                "--approved-by looks like an agent",
                "name the person who said yes",
            )
        )

    current = (rec.get("enforcement") or {}).get("state")
    if current not in PROMOTABLE:
        hint = {
            "human_review": "a human_review record has no check to enforce",
            "retired": "a retired record is not enforced",
            "block": "already at block, the strongest state",
        }.get(current, "run mil prove first; only proven, shadow, and comment move up")
        raise MilError(mil_line(rec_path, f"cannot promote from {current!r}", hint))
    last_result = (rec.get("proof") or {}).get("last_result")
    if not is_proven_result(last_result):
        raise MilError(
            mil_line(
                rec_path,
                f"proof.last_result is {last_result!r}",
                f"run mil prove {rec.get('id', args.incident)} until it is red-on-bad, green-on-good",
            )
        )

    nxt = ROLLOUT[ROLLOUT.index(current) + 1]
    target = args.to or nxt
    if target != nxt:
        raise MilError(
            mil_line(
                rec_path,
                f"{current} -> {target} skips a state",
                f"promote one step at a time; the next step is --to {nxt}",
            )
        )

    rid = str(rec.get("id", args.incident))
    if target == "block":
        _require_green_on_tree(root, rec_path, rid, rec)

    note = args.note.strip() if args.note else None
    if args.dry_run:
        extra = f"; {note}" if note else ""
        print(f"would promote {rid} {current} -> {target} (approved by {approved_by}{extra})")
        return 0

    confirm_promote(target, rid=rid, current=current)
    _apply(rec, target=target, current=current, approved_by=approved_by, note=note)
    save_record_file(rec_path, rec)
    print(f"promoted {rid} {current} -> {target} -> {rec_path}")
    return 0


def _apply(
    rec: dict[str, Any],
    *,
    target: str,
    current: str,
    approved_by: str,
    note: str | None,
) -> None:
    enforcement = rec.get("enforcement")
    if not isinstance(enforcement, dict):
        enforcement = {}
        rec["enforcement"] = enforcement
    enforcement["state"] = target
    hist = rec.get("history")
    if not isinstance(hist, list):
        hist = []
        rec["history"] = hist
    hist.append(
        history_entry(
            target=target,
            current=current,
            approved_by=approved_by,
            at=today(),
            note=note,
        )
    )


def _require_green_on_tree(root: Path, rec_path: Path, rid: str, rec: dict[str, Any]) -> None:
    """Refuse promotion to `block` when the check is red or broken on the product tree."""
    from mil.cmd_ci import _job_for_check, _load_waivers

    found, _ = discover_checks(root)
    match = [(prog, meta) for prog, meta in found if meta.get("incident") == rid]
    if not match:
        print(f"warning: no check with incident {rid} found; not run against the tree")
        return

    cfg_path = config_path(root)
    cfg_data: dict[str, Any] | None = None
    if cfg_path.is_file():
        try:
            loaded = yamlio.load_path(str(cfg_path))
            if isinstance(loaded, dict):
                cfg_data = loaded
        except yamlio.YAMLError:
            cfg_data = None

    as_block = copy.deepcopy(rec)
    as_block.setdefault("enforcement", {})["state"] = "block"
    waivers = _load_waivers(root)
    for program, meta in match:
        if (meta.get("input") or "tree") == "action":
            continue
        job = _run_as_block(
            root,
            program,
            meta,
            rid,
            as_block,
            waivers,
            cfg_data,
            _job_for_check,
        )
        if job is not None:
            _refuse_if_red(rec_path, job)


def _run_as_block(
    root: Path,
    program: Path,
    meta: dict[str, Any],
    rid: str,
    as_block: dict[str, Any],
    waivers: list[dict[str, Any]],
    cfg_data: dict[str, Any] | None,
    job_for_check: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    saved = {k: os.environ.get(k) for k in ("MIL_PR_BRANCH", "PYTHONDONTWRITEBYTECODE")}
    tmp: str | None = None
    try:
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        base: Path | None = None
        head: Path | None = None
        target_root = root
        if (meta.get("input") or "tree") == "diff":
            if not _is_git_root_with_head(root):
                print(
                    f"warning: {rid} is a diff check and {root} is not the top of a git work tree "
                    "with a HEAD commit; not run against the tree. Confirm it is green before block."
                )
                return None
            tmp = tempfile.mkdtemp(prefix="mil-promote-")
            tmp_path = Path(tmp)
            base = tmp_path / "base"
            head = tmp_path / "head"
            materialize_ref(root, "HEAD", base)
            materialize_staged(root, head)
            target_root = head
            if not os.environ.get("MIL_PR_BRANCH"):
                branch = current_branch(root)
                if branch:
                    os.environ["MIL_PR_BRANCH"] = branch
        return job_for_check(
            target_root,
            program,
            meta,
            {rid: as_block},
            base,
            head,
            config_path(root),
            waivers,
            config_data=cfg_data,
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _is_git_root_with_head(root: Path) -> bool:
    git = shutil.which("git")
    if not git:
        return False
    proc = subprocess.run(
        [git, "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    if Path(proc.stdout.strip()).resolve() != root.resolve():
        return False
    return has_head(root)


def _refuse_if_red(rec_path: Path, job: dict[str, Any]) -> None:
    if job.get("result") == "error":
        raise MilError(
            mil_line(
                rec_path,
                f"the check errors on the current tree: {job.get('detail')}",
                "a check that cannot run would fail every commit at block; fix it first",
            )
        )
    blocking = [h for h in (job.get("hits") or []) if h.get("state") == "block"]
    if blocking:
        lines = "; ".join(
            f"{h['path']}{':' + str(h['line']) if h.get('line') else ''}: {h['message']}"
            for h in blocking[:3]
        )
        more = f" (+{len(blocking) - 3} more)" if len(blocking) > 3 else ""
        raise MilError(
            mil_line(
                rec_path,
                f"the check is red on the current tree, so block would fail the next commit and CI: {lines}{more}",
                "make the check green on the product tree (or remove stray --bad/--good fixture copies), then promote",
            )
        )
