"""Argument parser for `mil`."""

from __future__ import annotations

import argparse
import sys

from mil import CONTRACT
from mil.cmd_ci import add_parser as add_ci
from mil.cmd_classify import add_parser as add_classify
from mil.cmd_demote import add_parser as add_demote
from mil.cmd_events import add_parser as add_events
from mil.cmd_guard import add_parser as add_guard
from mil.cmd_link import add_parser as add_link
from mil.cmd_lint import add_parser as add_lint
from mil.cmd_promote import add_parser as add_promote
from mil.cmd_prove import add_parser as add_prove
from mil.cmd_record import add_parser as add_record
from mil.cmd_retire import add_parser as add_retire
from mil.cmd_scaffold import add_parser as add_scaffold
from mil.cmd_status import add_parser as add_status
from mil.errors import MilError, mil_line
from mil.paths import contract_doc_rel, resolve_root

ROOT_ARGS = argparse.ArgumentParser(add_help=False)
ROOT_ARGS.add_argument(
    "--root",
    default=argparse.SUPPRESS,
    metavar="DIR",
    help="repository root that holds .make-it-lint/ (default: .)",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mil",
        description="Turn agent mistakes into proven, enforced repository checks.",
        epilog=f"Contract: {CONTRACT}. See {contract_doc_rel()}.",
    )
    parser.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="repository root that holds .make-it-lint/ (default: .)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the make-it-lint contract version this mil speaks",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    add_record(sub, ROOT_ARGS)
    add_classify(sub, ROOT_ARGS)
    add_scaffold(sub, ROOT_ARGS)
    add_link(sub, ROOT_ARGS)
    add_prove(sub, ROOT_ARGS)
    add_promote(sub, ROOT_ARGS)
    add_demote(sub, ROOT_ARGS)
    add_retire(sub, ROOT_ARGS)
    add_status(sub, ROOT_ARGS)
    add_lint(sub, ROOT_ARGS)
    add_ci(sub, ROOT_ARGS)
    add_guard(sub, ROOT_ARGS)
    add_events(sub, ROOT_ARGS)
    return parser


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if callable(reconf) and enc != "utf8":
            try:
                reconf(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(CONTRACT)
        return 0
    if not args.command:
        parser.print_help()
        return 2
    try:
        args.root = resolve_root(args.root)
        return int(args.func(args))
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    except MilError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        print(exc, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI edge: show the error, do not dump a traceback by default
        print(mil_line(exc), file=sys.stderr)
        return 2
