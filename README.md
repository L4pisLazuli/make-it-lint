# make-it-lint

`make-it-lint` turns a mistake an AI coding agent actually made into a
proven lint rule, PreToolUse hook, git pre-commit check, or CI check, and
records the incident in an auditable ledger.

A prose warning in `AGENTS.md` decays by the next session. A record in
`.make-it-lint/ledger/`, an executable check in `.make-it-lint/checks/`,
and `mil prove` do not.

Self-contained (`make-it-lint/v1`), POSIX `sh` + Python 3.10+ standard
library only, zero third-party packages, zero network calls inside `mil`.
MIT license; see `LICENSE`.

## Quickstart

### 1. Install into your repository

Clone `make-it-lint` once and run `mil-setup` against your target git
repository (requires Python 3.10+ and `git`):

```sh
git clone https://github.com/L4pisLazuli/make-it-lint.git

# macOS / Linux:
make-it-lint/bin/mil-setup --hooks /path/to/your-repo

# Windows (PowerShell or cmd.exe):
make-it-lint\bin\mil-setup --hooks C:\path\to\your-repo
```

> **Note:** If your repo has no `AGENTS.md`, `mil-setup` prints
> `skip AGENTS.md`. This is harmless.
>
> The first commit after install will show pre-commit warnings
> (`guard kit_modified` for the newly added kit files, and
> `waivers.yaml added`). These are expected on the install commit,
> and the commit still succeeds.

Commit the installed kit, ledger skeleton, hooks, and GitHub Actions workflow:

```sh
cd /path/to/your-repo
git add -A && git commit -m "chore: install make-it-lint"
```

### 2. Turn a mistake into a proven check

#### Option A: With Claude Code or Cursor (everyday workflow)

You do not need to memorize `mil` flags. When an agent makes a mistake in
a session, PR review, or CI run, invoke the installed skill:

```text
/make-it-lint Don't let agents commit debug breakpoint() calls in src/ again.
```

The agent records the incident in `.make-it-lint/ledger/` (extracting
`bad/` and `good/` from commits or minimal snapshots and auto-selecting
the earliest mechanical stop layer), runs `mil scaffold` to synthesize a
provisional check (provisional draft) under `.make-it-lint/checks/`, refines it if
needed, runs `mil prove --check-hunks` (`open -> proven`), and stops to
ask whether you want to enable it.

#### Option B: From the CLI (copy-paste walkthrough)

Run the following inside `/path/to/your-repo` (on Windows PowerShell /
`cmd.exe`, replace `bin/mil` with `bin\mil`). If the broken and fixed
states are already two git commits, pass `--bad-ref HEAD~1 --good-ref HEAD --auto`
instead of creating temporary fixture directories:

```sh
# 1. Create temporary broken (bad/) and fixed (good/) snapshots outside the repo
TMP_FX=$(mktemp -d)
mkdir -p "$TMP_FX/bad/src" "$TMP_FX/good/src"
printf 'def handle(req):\n    breakpoint()\n    return req\n' > "$TMP_FX/bad/src/handler.py"
printf 'def handle(req):\n    return req\n' > "$TMP_FX/good/src/handler.py"

# 2. Record the incident; --auto picks kind, decidability, and stop layer from bad/ vs good/
bin/mil record \
  --fingerprint lint:leftover-breakpoint \
  --title "Left breakpoint() in request handler" \
  --what "Agent committed a debug breakpoint() call in src/handler.py." \
  --source "PR review" --actor cloud-agent --reporter @you \
  --bad "$TMP_FX/bad" --good "$TMP_FX/good" \
  --auto
rm -rf "$TMP_FX"

# 3. Synthesize a provisional check (self-verified against bad/ and good/, kept in state=open)
INCIDENT=$(ls -1 .make-it-lint/ledger | grep '^F-' | tail -n 1)
bin/mil scaffold "$INCIDENT" --id MIL-BP-001 --path .make-it-lint/checks/no_breakpoint.py
```

Review `.make-it-lint/checks/no_breakpoint.py` (for structural AST,
config, diff-scope, and hook rules, `mil scaffold` drafts a working
provisional check and verifies `red-on-bad, green-on-good`; for
behavioral or property rules, or with `--no-draft`, it writes an `exit 2`
template ready for you to edit), then lock the proof:

```sh
bin/mil prove "$INCIDENT" --check-hunks
bin/mil lint
```

### 3. Enable rollout in your own terminal

Preview first with `mil promote --dry-run` (e.g.
`would promote proven -> shadow`). The real `promote` is a
human-only step.

Agents can never promote checks (`PreToolUse` and `mil promote` refuse
agent runtimes and non-TTY stdin). Run `mil promote` yourself and type
the target state at the prompt:

```sh
bin/mil promote "$INCIDENT" --approved-by @you
# Type "shadow" to confirm
```

- `shadow` — runs in CI / hooks and logs hits to `events.ndjson`; never blocks or comments on PRs.
- `comment` — posts GitHub warnings / PR comments and hook warnings; never fails CI or commits.
- `block` — blocks `PreToolUse`, `git commit`, and CI (`mil promote` verifies the product tree is green first).

## Try the bundled sample repository

Want to explore without installing anything first? `examples/sample-repo/`
ships with seven anonymized incidents and six proven checks (`tree`,
`diff`, `action`, and seeded `verify` checks):

```console
$ bin/mil lint --root examples/sample-repo
mil lint: ok (7 records, 6 checks, 1 waiver, make-it-lint/v1)

$ bin/mil prove --all --dry-run --root examples/sample-repo
ok   F-20260311-01 MIL-SCOPE-001: red-on-bad, green-on-good
ok   F-20260402-01 MIL-BASELINE-001: red-on-bad, green-on-good
ok   F-20260518-01 MIL-SUITE-001: red-on-bad, green-on-good
ok   F-20260609-01 MIL-GEN-001: red-on-bad, green-on-good
ok   F-20260814-01 MIL-PACK-001: red-on-bad 2/2, green-on-good 2/2
ok   F-20260905-01 MIL-HOOK-001: red-on-bad, green-on-good
mil prove: 6 of 6 proved

$ bin/mil status --root examples/sample-repo
open          1
proven        4
shadow        1
comment       1
block         0
human_review  0
retired       0
total         7
...
```

`mil status` prints a count per state, followed by a list of each record.

- `F-20260722-01` stays `open` with `layer: null` and a `layer_reason`
  because the disallowed default lives in an external launcher outside
  the repository.
- `F-20260814-01` (`MIL-PACK-001`) is a `layer: verify` property check
  with multiple `bad/` and `good/` variants and deterministic `MIL_SEED`.
- `F-20260905-01` (`MIL-HOOK-001`) is a `layer: hook`, `input: action`
  check that stops `git push --force` to protected branches during
  `PreToolUse` before the shell command executes.

## Installer & Hooks (`mil-setup`)

```sh
bin/mil-setup --dry-run path/to/repo  # preview without writing anything
bin/mil-setup path/to/repo            # install or upgrade kit files in path/to/repo
bin/mil-setup --hooks path/to/repo    # also wire git, Claude Code, and Cursor hooks
```

`mil-setup` (and `bin\mil-setup.cmd` on Windows) installs `bin/mil`,
`bin/mil.cmd`, `bin/mil-pre-commit`, `lib/mil/`, `schema/`,
`docs/contract.md`, the Claude Code and Cursor skills, the `.make-it-lint/`
skeleton, `.github/workflows/make-it-lint.yml`, and
`.github/scripts/pr-comment.py`. Re-running refreshes kit-owned files
while keeping your `.make-it-lint/` ledger, checks, config, waivers, and
`LICENSE`.

On Windows (`cmd.exe` or PowerShell), run `bin\mil` and `bin\mil-setup`
directly (`py -3`, `python3`, or `python` 3.10+). `.gitattributes` keeps
LF line endings on entry points and checks; if you add a new check on
Windows, stage it with `git add --chmod=+x .make-it-lint/checks/<file>.py`
(or run `bin/mil link` after `git add` to set `100755` in the git index).

### What `--hooks` wires

- **git `pre-commit`** (`bin/mil-pre-commit`): runs `mil ci --staged`
  against a clean materialization of `HEAD` and the staged git index
  (no `tar` required, untracked files never leak in). `block` hits or
  tampered ledger records stop the commit (`git commit --no-verify`
  skips once).
- **Claude Code (`.claude/settings.json`) & Cursor (`.cursor/hooks.json`)**:
  - `PreToolUse` / `preToolUse` -> `hooks/pre_tool_guard.py` (`mil guard --hook`):
    blocks direct agent edits to `.make-it-lint/waivers.yaml`, existing
    `record.yaml` files, proven/committed check scripts, kit-owned files,
    and `mil promote`, and runs active `input: action` checks. Uncommitted
    draft checks for `open` records remain editable until `mil prove`
    succeeds.
  - `PostToolUse` / `PostToolUseFailure` -> `hooks/session_log.py`:
    logs shell command outcomes to `.make-it-lint/cache/sessions/`.
  - `Stop` / `stop` -> `hooks/stop_suggest.py`: when a command or
    test/lint runner failed and later passed in the session, reminds the
    agent once per failure to offer `mil record` to the user (suggestion
    only; never blocks).

## CLI reference

All subcommands accept `--root <dir>`:

- `mil record` — create an `open` (or `human_review`) record, or append
  an occurrence on a matching `--fingerprint` (`--auto`,
  `--bad-ref REF --good-ref REF [--paths REL_PATH]`, `--from-ci`, `--bad`,
  `--good`, `--bad-variant NAME=PATH`, `--good-variant NAME=PATH`,
  `--evidence PATH`). Delete temporary `--bad` / `--good` source paths
  after recording so broken fixtures are never staged in the product tree.
- `mil classify` — classify or update `kind`, `decidable`, and `layer`
  on an `open` or `human_review` record (`--auto` infers them from
  `bad/` and `good/`).
- `mil scaffold` — synthesize a self-verified provisional check (or an
  `exit 2` manual template when `--no-draft` is passed or behavioral
  logic is required) under `.make-it-lint/checks/` (`.py` or
  `<dir>/{check.py, rule.yml}` for `--tool semgrep` / `--tool ast-grep`),
  set `+x`, and link it to an `open` record (`--auto` classifies first if
  needed).
- `mil link` — link an existing executable check (`incident: F-...`) to
  its record and ensure `+x` permissions.
- `mil prove` — run checks against `bad/` and `good/` (`--all`,
  `--dry-run`, `--verify-recorded`, `--check-hunks`, `--events`).
- `mil promote` — step `proven -> shadow -> comment -> block` at a human
  terminal (types the target state; refuses agent runtimes and refuses
  `block` when red on the current product tree; `--approved-by` required).
- `mil demote` — step `block -> comment -> shadow -> proven` on a false
  positive (`--reason` required).
- `mil retire` — move an obsolete record to `retired` (`--reason` required).
- `mil status` — summarize records by lifecycle state. Prints a count
  per state, followed by a list of each record.
- `mil lint` — validate schemas, history transitions, check metadata,
  git executable bits, fixture layout, waivers, and evidence `sha256`.
- `mil ci` — run product checks (`--staged`, `--base-ref`, `--head-ref`,
  `--base`/`--head`, `--json-summary`, `--events`).
- `mil guard` — enforce protected paths and `input: action` checks
  (`--hook`, `--action`, `--staged`, `--base-ref`).
- `mil events --summary` — aggregate `shadow_runs`, `hits`, `waivers`,
  `errors`, and `proves` across `.ndjson` files or artifact directories.

## Self-check

```sh
sh tests/selftest.sh
```

Runs syntax checks, contract checks, `mil lint`, `mil prove`,
weakened-check red-proofs, and the full `unittest` suite.
