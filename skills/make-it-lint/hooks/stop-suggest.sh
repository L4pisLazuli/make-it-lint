#!/bin/sh
# Stop hook entry point named in docs/contract.md.
#
# Delegates to stop_suggest.py when Python 3 is available; falls back to
# MIL_SESSION_LOG otherwise. Every path exits 0.

dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
for candidate in python3 python; do
	if command -v "$candidate" >/dev/null 2>&1 &&
		"$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
		"$candidate" "$dir/stop_suggest.py" || true
		exit 0
	fi
done

[ -t 0 ] || cat >/dev/null 2>&1
log=${MIL_SESSION_LOG-}
if [ -z "$log" ] || [ ! -s "$log" ]; then
	exit 0
fi
marker=$log.mil-suggested
if [ -e "$marker" ]; then
	exit 0
fi
: >"$marker" 2>/dev/null || exit 0
printf '%s\n' '{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "make-it-lint: something failed and was fixed in this session. If it was a mistake worth not repeating, offer the user to record it with the make-it-lint skill (mil record)."}}'
exit 0
