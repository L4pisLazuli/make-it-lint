"""Summarize append-only events.ndjson files or artifact directories."""

from __future__ import annotations

import argparse
from pathlib import Path

from mil.errors import MilError, mil_line
from mil.events import format_summary, load_events, summarize
from mil.paths import events_path, mil_dir

EVENTS_EPILOG = """\
Count shadow runs, hits, waivers, and check errors per check.

Pass one or more .ndjson files or directories of downloaded CI artifacts.
With no path, reads .make-it-lint/events.ndjson.

examples:
  mil events --summary
"""


def add_parser(sub: argparse._SubParsersAction, parents: argparse.ArgumentParser | None = None) -> None:
    extra = [parents] if parents is not None else []
    p = sub.add_parser(
        "events",
        help="summarize events.ndjson (shadow runs, hits, waivers, errors)",
        description="Summarize events.ndjson (shadow runs, hits, waivers, errors).",
        epilog=EVENTS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=extra,
    )
    p.add_argument(
        "--summary",
        nargs="*",
        default=None,
        metavar="PATH",
        help="print counts across PATH(s); defaults to .make-it-lint/events.ndjson",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    if args.summary is None:
        raise MilError(mil_line("events", "pass --summary, optionally with files or directories"))
    if args.summary:
        paths = []
        for raw in args.summary:
            p = Path(raw)
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            paths.append(p)
    else:
        if not mil_dir(root).is_dir():
            print("mil events: no events")
            return 0
        paths = [events_path(root)]
    combined = []
    for path in paths:
        combined.extend(load_events(path))
    print(format_summary(summarize(combined)))
    return 0
