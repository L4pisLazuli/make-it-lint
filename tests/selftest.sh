#!/bin/sh
# The kit's own checks, in the order .github/workflows/self.yml runs them.
#
#   tests/selftest.sh            run every step
#   tests/selftest.sh prove      run one step
#
# Steps: syntax lint prove weakened orphan contract unit

set -eu
cd "$(dirname "$0")/.."
SAMPLE=examples/sample-repo
MIL=bin/mil
export PYTHONPATH="$PWD/lib${PYTHONPATH:+:$PYTHONPATH}"
export MIL_TODAY="${MIL_TODAY:-2026-11-16}"

tmpdirs=
cleanup() {
	for d in $tmpdirs; do rm -rf "$d"; done
}
trap cleanup EXIT

copy_sample() {
	d=$(mktemp -d)
	tmpdirs="$tmpdirs $d"
	cp -R "$SAMPLE/." "$d"
	echo "$d"
}

step_syntax() {
	sh -n bin/mil
	sh -n bin/mil-setup
	sh -n bin/mil-pre-commit
	sh -n skills/make-it-lint/hooks/stop-suggest.sh
	sh -n tests/selftest.sh
	python3 -m py_compile lib/mil/*.py tests/*.py
	python3 -m py_compile examples/sample-repo/.make-it-lint/checks/*.py
	python3 -m py_compile examples/sample-repo/.make-it-lint/checks/suite_exit/check.py
	python3 -m py_compile tests/fixtures/generic-origin/.make-it-lint/checks/draft_in_published/check.py
	python3 -m py_compile skills/make-it-lint/hooks/pre_tool_guard.py skills/make-it-lint/hooks/session_log.py skills/make-it-lint/hooks/stop_suggest.py
	python3 -m py_compile .github/scripts/pr-comment.py
	if command -v shellcheck >/dev/null 2>&1; then
		shellcheck bin/mil bin/mil-setup bin/mil-pre-commit skills/make-it-lint/hooks/stop-suggest.sh tests/selftest.sh
	else
		echo "shellcheck not installed; ran sh -n only"
	fi
}

step_lint() {
	"$MIL" lint --root "$SAMPLE"
	# The pinned date is for the sample. This repository's own waivers
	# must expire by the real clock.
	(
		unset MIL_TODAY
		"$MIL" lint --root .
	)
}

step_prove() {
	"$MIL" prove --all --dry-run --verify-recorded --root "$SAMPLE"
	# Prove a copy for real and confirm zero drift against committed records.
	d=$(copy_sample)
	PYTHONDONTWRITEBYTECODE=1 "$MIL" prove --all --root "$d" >/dev/null
	diff -r "$SAMPLE/.make-it-lint/ledger" "$d/.make-it-lint/ledger"
}

weaken() {
	awk '{ print } /^# ---$/ && ++n == 2 { exit }' "$1" >"$1.new"
	printf '%s\n' "$2" >>"$1.new"
	mv "$1.new" "$1"
	chmod +x "$1"
}

step_weakened() {
	d=$(copy_sample)
	weaken "$d/.make-it-lint/checks/scope_paths.py" 'raise SystemExit(0)'
	if "$MIL" prove --all --dry-run --root "$d" >"$d/out"; then
		cat "$d/out"
		echo "selftest: a check that never fires was proved" >&2
		exit 1
	fi
	grep -q 'FAIL F-20260311-01 MIL-SCOPE-001: green-on-bad, green-on-good' "$d/out"

	d=$(copy_sample)
	weaken "$d/.make-it-lint/checks/suite_exit/check.py" 'print("run_suite.py:1: always fails"); raise SystemExit(1)'
	if "$MIL" prove --all --dry-run --root "$d" >"$d/out"; then
		cat "$d/out"
		echo "selftest: a check that always fires was proved" >&2
		exit 1
	fi
	grep -q 'FAIL F-20260518-01 MIL-SUITE-001: red-on-bad, red-on-good' "$d/out"
	echo "weakened checks fail the proof"
}

step_orphan() {
	d=$(copy_sample)
	printf '#!/bin/sh\n# ---\n# id: ORPHAN-001\n# ---\nexit 0\n' >"$d/.make-it-lint/checks/orphan.sh"
	chmod +x "$d/.make-it-lint/checks/orphan.sh"
	if "$MIL" lint --root "$d" >"$d/out"; then
		cat "$d/out"
		echo "selftest: mil lint accepted a check with no incident and no origin" >&2
		exit 1
	fi
	grep -q 'has neither incident nor origin' "$d/out"
	echo "check without incident or origin rejected"
}

step_contract() {
	python3 -m unittest -v tests.test_contract
}

step_unit() {
	python3 -m unittest discover -s tests -t .
}

steps=${*:-syntax lint prove weakened orphan contract unit}
for s in $steps; do
	echo "== $s"
	"step_$s"
done
