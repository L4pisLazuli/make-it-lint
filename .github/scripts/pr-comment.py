#!/usr/bin/env python3
"""Post or update a pull-request comment from a mil ci JSON summary.

mil itself does not use the network. This script is the GitHub Actions
half: it reads mil-ci-summary.json and talks to the GitHub API with
GITHUB_TOKEN. No third-party packages.

Shadow-only runs stay silent on the pull request unless an earlier
<!-- mil-ci --> comment already exists and needs updating to reflect
resolved hits.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MARKER = "<!-- mil-ci -->"


def should_post_new_comment(data: dict) -> bool:
    """True when there is something actionable for human reviewers on the PR."""
    if int(data.get("comment_hits") or 0) > 0:
        return True
    if int(data.get("block_hits") or 0) > 0:
        return True
    if int(data.get("check_errors") or 0) > 0:
        return True
    if data.get("protected_changes"):
        return True
    for job in data.get("ran") or []:
        if job.get("result") == "error":
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: pr-comment.py mil-ci-summary.json", file=sys.stderr)
        return 2
    summary_path = Path(args[0])
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    number = os.environ.get("PR_NUMBER") or os.environ.get("GITHUB_PR_NUMBER")
    if not number:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if event_path and Path(event_path).is_file():
            try:
                event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                event = {}
            number = str((event.get("pull_request") or {}).get("number") or "")
    if not token or not repo or not number:
        print("skip: need GITHUB_TOKEN, GITHUB_REPOSITORY, and a pull request number")
        return 0
    if not summary_path.is_file():
        print(f"skip: no summary file {summary_path}")
        return 0
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"skip: cannot read summary: {exc}")
        return 0
    api = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    try:
        existing_id = _find_existing_comment(api, repo, str(number), token)
        if existing_id is None and not should_post_new_comment(data):
            print("skip: no comment/block hits, check errors, or governance changes")
            return 0
        body = render_comment(data)
        _upsert_comment(api, repo, str(number), token, body, existing_id=existing_id)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        print(f"github comment failed: HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        print(f"github comment failed: {exc.reason}")
    return 0


def render_comment(data: dict) -> str:
    ran = data.get("ran") or []
    skipped = data.get("skipped") or []
    protected = data.get("protected_changes") or []
    review = data.get("awaiting_human_review", 0)
    lines = [
        MARKER,
        "## make-it-lint",
        "",
        f"- comment hits: {data.get('comment_hits', 0)}",
        f"- block hits: {data.get('block_hits', 0)}",
        f"- shadow hits: {data.get('shadow_hits', 0)}",
        f"- checks run: {len(ran)}, skipped: {len(skipped)}",
        f"- awaiting human review: {review}",
        "",
    ]
    if protected:
        lines.append("### Governance & protected changes")
        lines.append("")
        lines.append("| severity | kind | path | detail |")
        lines.append("| --- | --- | --- | --- |")
        for item in protected:
            sev = str(item.get("severity") or "review")
            kind = str(item.get("kind") or "")
            path = str(item.get("path") or "")
            detail = str(item.get("detail") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {sev} | {kind} | `{path}` | {detail} |")
        lines.append("")
    if ran:
        lines.append("| check | incident | state | result | evidence | seed |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for job in ran:
            evidence = _evidence_cell(job.get("evidence") or [])
            seed = job.get("seed")
            seed_s = "" if seed is None else str(seed)
            lines.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (
                    job.get("id") or "",
                    job.get("incident") or "-",
                    job.get("effective_state") or job.get("state") or "",
                    job.get("result") or "",
                    evidence.replace("|", "\\|"),
                    seed_s,
                )
            )
        lines.append("")
    hits = []
    for job in ran:
        for hit in job.get("hits") or []:
            hit_state = hit.get("state") or job.get("effective_state") or ""
            if hit_state == "shadow":
                continue
            loc = hit.get("path") or ""
            if hit.get("line"):
                loc = f"{loc}:{hit['line']}"
            hits.append(
                (
                    hit_state,
                    job.get("id") or "",
                    loc,
                    hit.get("message") or "",
                )
            )
        if job.get("result") == "error":
            hits.append(
                (
                    job.get("effective_state") or job.get("state") or "",
                    job.get("id") or "",
                    job.get("path") or "",
                    job.get("detail") or "check error",
                )
            )
    if hits:
        lines.append("| state | check | location | message |")
        lines.append("| --- | --- | --- | --- |")
        for state, ident, loc, message in hits:
            msg = str(message).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {state} | {ident} | {loc} | {msg} |")
        lines.append("")
    else:
        lines.append("No comment or block hits.")
        lines.append("")
    if skipped:
        lines.append("<details><summary>skipped checks</summary>")
        lines.append("")
        for job in skipped:
            lines.append(f"- `{job.get('id')}`: {job.get('reason')}")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return "\n".join(lines)


def _evidence_cell(refs: list) -> str:
    items = [str(r) for r in refs if r]
    if len(items) <= 3:
        return ", ".join(items)
    return "%s (+%d)" % (", ".join(items[:3]), len(items) - 3)


def _find_existing_comment(api: str, repo: str, number: str, token: str) -> int | None:
    page = 1
    while page <= 20:
        comments = _request(
            "GET",
            f"{api}/repos/{repo}/issues/{number}/comments?per_page=100&page={page}",
            token,
        )
        if not isinstance(comments, list) or not comments:
            return None
        for item in comments:
            if isinstance(item, dict) and MARKER in str(item.get("body") or ""):
                cid = item.get("id")
                if isinstance(cid, int):
                    return cid
        if len(comments) < 100:
            return None
        page += 1
    return None


def _upsert_comment(
    api: str, repo: str, number: str, token: str, body: str, *, existing_id: int | None
) -> None:
    payload = json.dumps({"body": body}).encode("utf-8")
    if existing_id is not None:
        _request(
            "PATCH",
            f"{api}/repos/{repo}/issues/comments/{existing_id}",
            token,
            payload,
        )
        print(f"updated comment {existing_id}")
        return
    _request("POST", f"{api}/repos/{repo}/issues/{number}/comments", token, payload)
    print("posted comment")


def _request(method: str, url: str, token: str, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
