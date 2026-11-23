"""Read and write ledger records."""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Any

from mil import yamlio
from mil.errors import MilError
from mil.models import RECORD_ID_RE, ordered_incident
from mil.paths import ledger_dir

ID_RE = RECORD_ID_RE


def today() -> str:
    # MIL_TODAY pins the date so tests and examples do not depend on the clock.
    pinned = os.environ.get("MIL_TODAY")
    if pinned:
        try:
            return dt.date.fromisoformat(pinned).isoformat()
        except ValueError as exc:
            raise MilError(mil_today_error(pinned)) from exc
    return dt.date.today().isoformat()


def mil_today_error(pinned: str) -> str:
    return f"mil: MIL_TODAY={pinned!r} is not a YYYY-MM-DD date"


def parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def record_dir(root: Path, incident_id: str) -> Path:
    return ledger_dir(root) / incident_id


def record_path(root: Path, incident_id: str) -> Path:
    return record_dir(root, incident_id) / "record.yaml"


def load_record_file(path: Path) -> dict[str, Any]:
    data = yamlio.load_path(str(path))
    if not isinstance(data, dict):
        raise yamlio.YAMLError(f"{path}: record must be a mapping")
    return data


def save_record_file(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yamlio.dump_path(str(path), ordered_incident(record))


def iter_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    base = ledger_dir(root)
    if not base.is_dir():
        return found
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        rec = child / "record.yaml"
        if rec.is_file():
            found.append((rec, load_record_file(rec)))
    return found


def find_by_id(root: Path, incident_id: str) -> tuple[Path, dict[str, Any]] | None:
    path = record_path(root, incident_id)
    if not path.is_file():
        return None
    return path, load_record_file(path)


def find_by_fingerprint(root: Path, fingerprint: str) -> tuple[Path, dict[str, Any]] | None:
    hits = []
    for path, rec in iter_records(root):
        if rec.get("fingerprint") == fingerprint:
            hits.append((path, rec))
    if not hits:
        return None
    if len(hits) > 1:
        ids = ", ".join(str(rec.get("id")) for _, rec in hits)
        raise yamlio.YAMLError(f"fingerprint {fingerprint!r} matches more than one record: {ids}")
    return hits[0]


def next_id(root: Path, when: str | None = None) -> str:
    day = when or today()
    compact = day.replace("-", "")
    if not re.fullmatch(r"[0-9]{8}", compact):
        raise yamlio.YAMLError(f"invalid date for id: {day!r}")
    used = []
    for _, rec in iter_records(root):
        match = ID_RE.fullmatch(str(rec.get("id", "")))
        if match and match.group(1) == compact:
            used.append(int(match.group(2)))
        else:
            match = ID_RE.fullmatch(Path(record_dir(root, str(rec.get("id", "x")))).name)
            if match and match.group(1) == compact:
                used.append(int(match.group(2)))
    n = max(used, default=0) + 1
    return f"F-{compact}-{n:02d}"


def default_promote_when() -> str:
    return "2 weeks or 10 shadow runs, 0 false positives; human OK required to promote"
