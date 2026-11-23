"""Protected-path enforcement and PreToolUse action guard.

make-it-lint/v1 folds governance into mil itself so adopter repositories
do not need a separate external harness to guard waivers, existing
checks, ledger records, or tool calls.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from mil import yamlio
from mil.checks import (
    CheckError,
    discover_checks,
    extract_metadata,
    invoke_check_reproducible,
    resolve_check_program,
)
from mil.events import append_event
from mil.ledger import find_by_id, iter_records, load_record_file, parse_date, today
from mil.models import RUNS_ON_PRODUCT, parse_hit_line
from mil.paths import checks_dir, config_path, is_within_root, normalize_rel_path, waivers_path

KIT_OWNED_PREFIXES = (
    "bin/mil",
    "bin/mil.cmd",
    "bin/mil-pre-commit",
    "lib/mil",
    "schema",
    "docs/contract.md",
)

FILE_WRITE_TOOLS = {
    "Edit",
    "MultiEdit",
    "Write",
    "NotebookEdit",
    "StrReplace",
    "CreateFile",
    "WriteFile",
}
FILE_DELETE_TOOLS = {
    "Delete",
    "DeleteFile",
    "RemoveFile",
}
COMMAND_TOOLS = {
    "Bash",
    "Shell",
    "RunCommand",
    "Terminal",
}


def is_kit_source_repo(root: Path) -> bool:
    """True when `root` is make-it-lint itself rather than an adopter repo."""
    return (root / "bin" / "mil-setup").is_file() and (root / "tests" / "selftest.sh").is_file()


_PY_BIN_RE = re.compile(r"^(?:python[0-9.]*|py)(?:\.exe)?$", re.IGNORECASE)
_SHELL_WRAPPERS = {"sh", "bash", "dash", "zsh", "env", "cmd", "cmd.exe"}
_PROMOTE_VALUE_FLAGS = {"--to", "--approved-by", "--note", "--root"}


def _split_shell_segments(command: str) -> list[str]:
    """Split a shell command string on unquoted `;`, `|`, `&`, or newline."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i : i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in (";", "|", "&", "\n"):
            seg = "".join(buf).strip()
            if seg:
                segments.append(seg)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _segment_tokens(segment: str) -> list[str]:
    try:
        raw = shlex.split(segment, posix=False)
    except ValueError:
        raw = segment.split()
    return [_unquote(tok) for tok in raw if tok]


def _segment_is_live_mil_promote(tokens: list[str]) -> bool:
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if "=" in tok and not tok.startswith("-"):
            idx += 1
            continue
        base = re.split(r"[/\\]", tok)[-1].lower()
        if base in _SHELL_WRAPPERS:
            idx += 1
            while idx < n and tokens[idx].lower() in ("/c", "/d", "-c"):
                if tokens[idx].lower() == "-c" and idx + 1 < n:
                    return _is_live_mil_promote(tokens[idx + 1])
                idx += 1
            continue
        break

    if idx >= n:
        return False

    exe_base = re.split(r"[/\\]", tokens[idx])[-1].lower()
    if exe_base in ("mil", "mil.cmd"):
        idx += 1
    elif _PY_BIN_RE.match(exe_base):
        idx += 1
        while idx < n and tokens[idx].startswith("-") and tokens[idx] != "-m":
            idx += 1
        if idx + 1 >= n or tokens[idx] != "-m" or tokens[idx + 1] != "mil":
            return False
        idx += 2
    else:
        return False

    while idx < n and tokens[idx].startswith("-"):
        if tokens[idx] == "--root":
            idx += 2
        elif tokens[idx].startswith("--root="):
            idx += 1
        else:
            break

    if idx >= n or tokens[idx] != "promote":
        return False
    idx += 1

    while idx < n:
        tok = tokens[idx]
        if tok in ("--dry-run", "--help", "-h"):
            return False
        if tok in _PROMOTE_VALUE_FLAGS:
            idx += 2
            continue
        idx += 1
    return True


def _is_live_mil_promote(command: str) -> bool:
    return any(_segment_is_live_mil_promote(_segment_tokens(seg)) for seg in _split_shell_segments(command))


def matches_pattern(rel_path: str, pattern: str) -> bool:
    norm_path = normalize_rel_path(rel_path)
    norm_pat = normalize_rel_path(pattern)
    if not norm_pat:
        return False
    if norm_path == norm_pat or norm_path.startswith(norm_pat + "/"):
        return True
    if norm_pat.endswith("/**"):
        base = norm_pat[:-3].rstrip("/")
        if norm_path == base or norm_path.startswith(base + "/"):
            return True
    return fnmatch.fnmatchcase(norm_path, norm_pat)


def normalize_hook_payload(payload: dict[str, Any], root: Path) -> dict[str, Any]:
    """Normalize Claude Code or Cursor PreToolUse JSON into an action.json dict."""
    tool = str(payload.get("tool_name") or payload.get("tool") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    command = ""
    raw_paths: list[str] = []
    kind = "other"

    if tool in COMMAND_TOOLS or "command" in tool_input:
        kind = "command"
        command = str(tool_input.get("command") or tool_input.get("cmd") or "").strip()
    elif tool in FILE_DELETE_TOOLS:
        kind = "delete"
    elif tool in FILE_WRITE_TOOLS:
        kind = "write"

    for key in ("file_path", "path", "notebook_path", "target_file", "destination"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            raw_paths.append(val.strip())
            if kind == "other":
                kind = "write"
    paths_val = tool_input.get("paths")
    if isinstance(paths_val, list):
        for item in paths_val:
            if isinstance(item, str) and item.strip():
                raw_paths.append(item.strip())

    rel_paths: list[str] = []
    for raw in raw_paths:
        p = Path(raw)
        if p.is_absolute():
            if is_within_root(root, p):
                rel = normalize_rel_path(str(p.resolve().relative_to(root.resolve())))
            else:
                rel = p.as_posix()
        else:
            rel = normalize_rel_path(raw)
        if rel and rel not in rel_paths:
            rel_paths.append(rel)

    return {
        "tool": tool,
        "kind": kind,
        "command": command,
        "path": rel_paths[0] if rel_paths else "",
        "paths": rel_paths,
        "tool_input": tool_input,
    }


def check_protected_write(
    root: Path,
    rel_path: str,
    *,
    kind: str,
    config_data: dict[str, Any] | None = None,
) -> str | None:
    """Return a block reason if an agent tool call tries to write/delete a protected path."""
    if kind not in ("write", "delete"):
        return None
    norm = normalize_rel_path(rel_path)
    if not norm:
        return None

    if norm == ".make-it-lint/waivers.yaml":
        return (
            ".make-it-lint/waivers.yaml is human-only; ask a human to add or renew a waiver "
            "with their handle in approved_by"
        )

    if norm.startswith(".make-it-lint/ledger/") and norm.endswith("/record.yaml"):
        target = root / norm
        if kind == "delete" or target.is_file():
            return (
                f"{norm} is managed by mil CLI commands; use mil record, mil classify, "
                "mil link, mil prove, mil promote, mil demote, or mil retire instead of direct edits"
            )

    if norm.startswith(".make-it-lint/checks/") and norm != ".make-it-lint/checks/.gitkeep":
        target = root / norm
        rel_inside_str = norm[len(".make-it-lint/checks/") :]
        first_seg = rel_inside_str.split("/", 1)[0]
        top_entry = root / ".make-it-lint/checks" / first_seg
        if kind == "delete":
            return (
                f"{norm} already exists; modifying or deleting an existing check requires human review "
                "(add a new check file or ask the user to edit it)"
            )
        if (target.exists() or ("/" in rel_inside_str and top_entry.exists())) and not _is_editable_open_draft_check(
            root, norm, target, top_entry=top_entry
        ):
            return (
                f"{norm} already exists; modifying or deleting an existing check requires human review "
                "(add a new check file or ask the user to edit it)"
            )

    if not is_kit_source_repo(root):
        for prefix in KIT_OWNED_PREFIXES:
            if matches_pattern(norm, prefix):
                return f"{norm} is kit-owned; upgrade make-it-lint via bin/mil-setup rather than editing it directly"

    custom = (config_data or {}).get("protected_paths")
    if isinstance(custom, list):
        for pattern in custom:
            if isinstance(pattern, str) and matches_pattern(norm, pattern):
                return f"{norm} matches protected_paths pattern {pattern!r} in .make-it-lint/config.yaml"

    return None


def _is_editable_open_draft_check(
    root: Path, norm: str, target: Path, *, top_entry: Path | None = None
) -> bool:
    """Allow iterating on an uncommitted draft check while its incident record is still `open`."""
    cdir = checks_dir(root)
    try:
        rel_inside = target.resolve().relative_to(cdir.resolve())
    except ValueError:
        return False
    entry = top_entry if top_entry is not None else (cdir / rel_inside.parts[0] if len(rel_inside.parts) > 1 else target)
    if not entry.exists() or (len(rel_inside.parts) == 1 and not target.is_file()):
        return False

    entry_norm = f".make-it-lint/checks/{rel_inside.parts[0]}"
    has_git_head = False
    git = shutil.which("git")
    if git:
        head_proc = subprocess.run(
            [git, "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
        )
        if head_proc.returncode == 0:
            has_git_head = True
            for git_obj in {norm, entry_norm}:
                cat_proc = subprocess.run(
                    [git, "-C", str(root), "cat-file", "-e", f"HEAD:{git_obj}"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=5,
                )
                if cat_proc.returncode == 0:
                    return False

    if entry.name.startswith((".", "_")):
        return has_git_head

    try:
        program = resolve_check_program(entry)
        meta = extract_metadata(program)
    except (CheckError, yamlio.YAMLError):
        return has_git_head

    if meta.get("origin"):
        return False
    incident_id = meta.get("incident")
    if not isinstance(incident_id, str) or not incident_id.strip():
        return has_git_head
    found = find_by_id(root, incident_id.strip())
    if not found:
        return False
    _, rec = found
    return (rec.get("enforcement") or {}).get("state") == "open"


def evaluate_action(
    root: Path,
    action: dict[str, Any],
    *,
    config_data: dict[str, Any] | None = None,
    sinks: list[Path] | None = None,
) -> dict[str, Any]:
    """Check protected paths and run active `input: action` checks on `action`."""
    cfg_path = config_path(root)
    if config_data is None and cfg_path.is_file():
        try:
            loaded = yamlio.load_path(str(cfg_path))
            if isinstance(loaded, dict):
                config_data = loaded
        except yamlio.YAMLError:
            config_data = None

    blocks: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    shadows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    kind = str(action.get("kind") or "other")
    cmd_text = str(action.get("command") or "")
    if kind == "command" and _is_live_mil_promote(cmd_text):
        blocks.append(
            {
                "check": "MIL-GUARD-PROMOTE",
                "incident": "",
                "path": ".make-it-lint/ledger",
                "line": None,
                "message": (
                    "mil promote must be run by a human in their own terminal; "
                    "print the command for the user instead (or pass --dry-run to preview)"
                ),
                "state": "block",
            }
        )

    paths = [str(p) for p in (action.get("paths") or []) if p]
    if not paths and action.get("path"):
        paths = [str(action["path"])]

    for rel in paths:
        reason = check_protected_write(root, rel, kind=kind, config_data=config_data)
        if reason:
            blocks.append(
                {
                    "check": "MIL-GUARD-PROTECTED",
                    "incident": "",
                    "path": rel,
                    "line": None,
                    "message": reason,
                    "state": "block",
                }
            )

    records = {rec.get("id"): rec for _, rec in iter_records(root)}
    waivers = _load_active_waivers(root)
    found, discover_errs = discover_checks(root)
    for child, exc in discover_errs:
        errors.append({"check": child.name, "detail": str(exc)})

    action_checks: list[tuple[Path, dict[str, Any], dict[str, Any] | None, str]] = []
    for program, meta in found:
        if (meta.get("input") or "tree") != "action":
            continue
        incident = meta.get("incident")
        rec = records.get(incident) if incident else None
        if incident and rec is None:
            errors.append(
                {
                    "check": str(meta.get("id") or program.name),
                    "detail": f"incident {incident} has no record in .make-it-lint/ledger/",
                }
            )
            continue
        if rec is not None:
            state = str((rec.get("enforcement") or {}).get("state") or "")
        else:
            state = str(((meta.get("enforcement") or {}).get("state")) or "shadow")
        if state in RUNS_ON_PRODUCT:
            action_checks.append((program, meta, rec, state))

    if action_checks:
        with tempfile.TemporaryDirectory(prefix="mil-action-") as tmp:
            action_file = Path(tmp) / "action.json"
            action_file.write_text(json.dumps(action, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            for program, meta, rec, state in action_checks:
                ident = str(meta.get("id") or program.name)
                incident = str(meta.get("incident") or "")
                layer = str((rec or {}).get("layer") or meta.get("layer") or "hook")
                seed = _seed_of(rec, meta)
                try:
                    proc = invoke_check_reproducible(
                        program,
                        meta,
                        layer=layer,
                        seed=seed,
                        root=root,
                        config=cfg_path,
                        action_file=action_file,
                        config_data=config_data,
                    )
                except (CheckError, Exception) as exc:  # noqa: BLE001
                    errors.append({"check": ident, "detail": str(exc)})
                    _emit(sinks, root, "check_error", ident, incident, "", str(exc))
                    continue

                if state == "shadow":
                    _emit(sinks, root, "shadow_run", ident, incident, "", "ran")

                rc = proc.returncode
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                if rc not in (0, 1):
                    detail = (stderr or stdout or f"exit {rc}").strip()
                    errors.append({"check": ident, "detail": detail})
                    _emit(sinks, root, "check_error", ident, incident, "", detail)
                    continue

                for line in stdout.splitlines():
                    if not line.strip():
                        continue
                    parsed = parse_hit_line(line)
                    if parsed is None:
                        if rc == 1:
                            detail = f"stdout not in '<path>[:<line>]: <message>' form: {line!r}"
                            errors.append({"check": ident, "detail": detail})
                            _emit(sinks, root, "check_error", ident, incident, "", detail)
                        continue
                    hit_path, lineno, message = parsed
                    effective = state
                    waived = False
                    if state == "block" and hit_waived(waivers, ident, hit_path):
                        effective = "comment"
                        waived = True
                    hit_obj = {
                        "check": ident,
                        "incident": incident,
                        "path": hit_path,
                        "line": lineno,
                        "message": message,
                        "state": effective,
                        "waived": waived,
                    }
                    if effective == "block":
                        blocks.append(hit_obj)
                        _emit(sinks, root, "block_hit", ident, incident, hit_path, message)
                    elif effective == "comment":
                        comments.append(hit_obj)
                        _emit(sinks, root, "comment_hit", ident, incident, hit_path, message)
                        if waived:
                            _emit(sinks, root, "waiver", ident, incident, hit_path, "block downgraded to comment")
                    else:
                        shadows.append(hit_obj)
                        _emit(sinks, root, "shadow_hit", ident, incident, hit_path, message)

    return {
        "decision": "deny" if (blocks or errors) else "allow",
        "blocks": blocks,
        "comments": comments,
        "shadows": shadows,
        "errors": errors,
    }


def audit_diff(
    base: Path,
    head: Path,
    *,
    root: Path | None = None,
    config_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Inspect protected/governance paths changed between `base` and `head`.

    Returns items with keys:
      - kind: waivers_modified | check_modified | state_transition | record_tampered | kit_modified | protected_path_modified
      - severity: review | block
      - path: repo-relative path
      - detail: human-readable summary
    """
    findings: list[dict[str, Any]] = []
    base_files = _tree_digests(base)
    head_files = _tree_digests(head)
    changed = sorted(set(base_files) | set(head_files))
    is_kit = is_kit_source_repo(root or head)
    custom = (config_data or {}).get("protected_paths")
    custom_patterns = [p for p in custom if isinstance(p, str)] if isinstance(custom, list) else []

    for rel in changed:
        b_hash = base_files.get(rel)
        h_hash = head_files.get(rel)
        if b_hash == h_hash:
            continue

        if rel == ".make-it-lint/waivers.yaml":
            action = "added" if b_hash is None else ("removed" if h_hash is None else "modified")
            findings.append(
                {
                    "kind": "waivers_modified",
                    "severity": "review",
                    "path": rel,
                    "detail": f"waivers.yaml {action}; verify human approval",
                }
            )
            continue

        if rel.startswith(".make-it-lint/checks/") and rel != ".make-it-lint/checks/.gitkeep":
            if b_hash is not None:
                action = "deleted" if h_hash is None else "modified"
                findings.append(
                    {
                        "kind": "check_modified",
                        "severity": "review",
                        "path": rel,
                        "detail": f"existing check {action}; verify fixtures and intent",
                    }
                )
            continue

        if rel.startswith(".make-it-lint/ledger/") and rel.endswith("/record.yaml"):
            if b_hash is None:
                continue
            if h_hash is None:
                findings.append(
                    {
                        "kind": "record_tampered",
                        "severity": "block",
                        "path": rel,
                        "detail": "existing ledger record deleted; use mil retire instead",
                    }
                )
                continue
            findings.extend(_audit_record_change(rel, base / rel, head / rel))
            continue

        if not is_kit:
            if any(matches_pattern(rel, pat) for pat in KIT_OWNED_PREFIXES):
                findings.append(
                    {
                        "kind": "kit_modified",
                        "severity": "review",
                        "path": rel,
                        "detail": "kit-owned file changed; verify this came from bin/mil-setup",
                    }
                )
                continue

        for pat in custom_patterns:
            if matches_pattern(rel, pat):
                findings.append(
                    {
                        "kind": "protected_path_modified",
                        "severity": "review",
                        "path": rel,
                        "detail": f"matches protected_paths pattern {pat!r}",
                    }
                )
                break

    return findings


def _audit_record_change(rel: str, base_rec_path: Path, head_rec_path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        old = load_record_file(base_rec_path)
        new = load_record_file(head_rec_path)
    except yamlio.YAMLError as exc:
        return [
            {
                "kind": "record_tampered",
                "severity": "block",
                "path": rel,
                "detail": f"invalid YAML in modified record: {exc}",
            }
        ]

    for key in ("id", "fingerprint"):
        if old.get(key) != new.get(key):
            out.append(
                {
                    "kind": "record_tampered",
                    "severity": "block",
                    "path": rel,
                    "detail": f"immutable field {key!r} changed ({old.get(key)!r} -> {new.get(key)!r})",
                }
            )

    old_hist = old.get("history") if isinstance(old.get("history"), list) else []
    new_hist = new.get("history") if isinstance(new.get("history"), list) else []
    if len(new_hist) < len(old_hist) or new_hist[: len(old_hist)] != old_hist:
        out.append(
            {
                "kind": "record_tampered",
                "severity": "block",
                "path": rel,
                "detail": "past history entries were rewritten or truncated",
            }
        )

    old_state = (old.get("enforcement") or {}).get("state")
    new_state = (new.get("enforcement") or {}).get("state")
    if old_state != new_state:
        by = ""
        if new_hist and isinstance(new_hist[-1], dict):
            who = new_hist[-1].get("approved_by") or new_hist[-1].get("by")
            if who:
                by = f" (by {who})"
        out.append(
            {
                "kind": "state_transition",
                "severity": "review",
                "path": rel,
                "detail": f"enforcement.state {old_state} -> {new_state}{by}",
            }
        )
    return out


def _tree_digests(tree: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not tree.is_dir():
        return out
    for path in tree.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(tree).as_posix()
        if rel == ".git" or rel.startswith(".git/") or "/__pycache__/" in f"/{rel}/":
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def hit_waived(waivers: list[dict[str, Any]], check_id: str, hit_path: str) -> bool:
    hit = normalize_rel_path(hit_path)
    for item in waivers:
        if item.get("check") != check_id:
            continue
        for prefix in item.get("paths") or []:
            if matches_pattern(hit, str(prefix)):
                return True
    return False


def _load_active_waivers(root: Path) -> list[dict[str, Any]]:
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


def _emit(
    sinks: list[Path] | None,
    root: Path,
    etype: str,
    check: str,
    incident: str,
    path: str,
    detail: str,
) -> None:
    if not sinks:
        return
    for sink in sinks:
        append_event(
            sink,
            {
                "type": etype,
                "check": check,
                "incident": incident,
                "path": path,
                "detail": detail,
            },
            root=root,
        )
