---
name: make-it-lint
description: Record a concrete agent mistake in the make-it-lint ledger and turn it into an executable check proven on bad/ and good/ fixtures. Use when the user invokes /make-it-lint; when code review points out an agent mistake; when the user says a mistake happened again ("again", "same mistake", "I told you before", "don't do that again", "never again"); when a CI job failed; or when the Stop hook reports a command that failed and was later fixed.
---

# make-it-lint (`make-it-lint/v1`)

Turn a real agent mistake into a ledger record and an executable check
that exits `1` on `bad/` and `0` on `good/`. Never "fix" a recurring
mistake by appending another prose bullet to a prompt.

## Governance boundaries

- You **may** create a new `.make-it-lint/ledger/F-YYYYMMDD-NN/` record
  via `mil record`, classify it via `mil classify`, scaffold or write a
  **new** executable check under `.make-it-lint/checks/` (`mil scaffold`
  creates and links the draft in one step, and draft checks remain
  editable while the record is `open` and uncommitted), link via
  `mil link`, and prove via `mil prove`.
- Do **not** hand-edit `.make-it-lint/waivers.yaml`, existing
  `record.yaml` files, or proven/committed check bodies (`PreToolUse` /
  `mil guard` blocks direct edits).
- Promotion (`proven -> shadow -> comment -> block`) is **never**
  automatic. Do **not** run `mil promote` yourself (`PreToolUse` /
  `mil guard` blocks it and `mil promote` refuses agent environments).
  After every successful `mil prove`, ask the user whether to enable the
  check; on an explicit yes, print
  `bin/mil promote F-... --approved-by @their-handle` so the user can
  run it in their own terminal and type the next state.

## Triggers

- **Manual**: `/make-it-lint` or explicit user request.
- **Code review**: a human review comment or `/code-review` finding on
  an agent mistake (`--human-words` + `--reporter`). Skip one-off style nits.
- **Repeated mistake**: user says "again", "same mistake", "I told you before",
  "never again", etc. Offer to record first; reuse the existing
  `--fingerprint` if already in the ledger so `mil record` appends an
  occurrence and prints a layer-raise suggestion.
- **CI log**: `bin/mil record --from-ci <local-log-or-yaml>` (local file
  only), followed by `bin/mil classify F-...`.
- **Stop hook suggestion**: when `hooks/stop_suggest.py` reports a
  command or test/lint runner that failed and later passed, briefly
  offer to record it if it came from a real mistake. It only suggests
  (never blocks). Never record without the user's yes.

## Workflow

Ask only for details not already clear from context.

1. **Record (`open` or `human_review`)**:
   Run `bin/mil record` with `--title`, `--fingerprint` (stable class ID
   like `lint:leftover-breakpoint` or `scope:path-outside-allowlist`),
   `--source`, `--actor`, `--reporter`, `--what`, and fixtures via either:
   - `--bad-ref <bad-commit> --good-ref <good-commit> [--paths <rel>] --auto`
     when the broken and fixed states exist in git history, or
   - `--bad` and `--good` (or `--bad-variant NAME=PATH` /
     `--good-variant NAME=PATH`, `--evidence PATH`) with `--auto` (or
     explicit `--kind`, `--decidable`, `--layer`, `--layer-reason`).
   - `--bad` and `--good` are inputs for recording only: `mil record`
     copies them into the ledger. Delete temporary source fixtures
     afterwards (or create them outside the product repo) and never
     `git add` them; a broken fixture left in the product tree trips
     `block` checks.
   - If `decidable` is `no`, `mil record --decidable no` sets
     `human_review`; stop here without writing a check.
2. **Pick or verify the earliest mechanical stop layer** (`reference/layers.md`):
   `architecture -> type -> lint -> test -> verify -> hook -> ci`.
   `--auto` (on `mil record`, `mil classify`, or `mil scaffold`) inspects
   `bad/` vs `good/` and repository linters (`ast-grep`, `semgrep`, etc.)
   to choose the earliest mechanical stop layer automatically; override
   with `--layer` whenever domain context calls for a stronger or more
   specific layer:
   - Disallowed module import boundary -> `architecture` (`input: tree`)
   - Type-escape suppression (`# type: ignore`, `@ts-ignore`, `as any`) -> `type` (`input: tree`)
   - Single file AST / config / forbidden path on disk -> `lint` (`input: tree`)
   - Test runner / baseline / fixture -> `test`
   - Seeded property / model / runtime check -> `verify`
   - Destructive shell command or edit before execution -> `hook` (`input: action`)
   - Multi-file diff, co-change, or PR kind -> `ci` (`input: diff`)
   - External launcher/UI invisible to the repo -> `--no-layer` with
     `--layer-reason` (record stays `open`).
3. **Draft (provisional draft) and refine the executable check** under `.make-it-lint/checks/`:
   Run `bin/mil scaffold F-... [--auto] [--tool ast-grep|semgrep] [--id MIL-...] [--path ...]`:
   - `mil scaffold` synthesizes a provisional check (`.py` stdlib check,
     or `<dir>/{check.py, rule.yml}` bundle for `ast-grep` / `semgrep`),
     dry-runs it against all `bad/` and `good/` variants while keeping
     `state=open`, and falls back to an `exit 2` manual template when
     behavioral/property logic is needed (or `--no-draft` is passed).
   - **Always inspect the drafted check** before proving so you confirm
     it captures the true failure invariant rather than overfitting a
     single literal, and edit it if needed (draft checks remain editable
     while `state=open` and uncommitted):

   ```python
   #!/usr/bin/env python3
   # ---
   # id: MIL-...
   # incident: F-YYYYMMDD-NN
   # input: tree   # tree | diff | action
   # layer: lint
   # tool: script
   # ---
   ```

   - Read per-check settings from `os.environ.get("MIL_CHECK_CONFIG_JSON")`
     with `json.loads` (stdlib-only, zero YAML parser needed!).
   - Invocations:
     - `tree`: `<check> --root <dir> --config <config.yaml>`
     - `diff`: `<check> --base <dir> --head <dir> --config <config.yaml>`
     - `action`: `<check> --action <action.json> --root <dir> --config <config.yaml>`
   - Exit `0` = clean, `1` = hit (`<path>[:<line>]: <message>` on stdout
     saying how to fix it), `2` = check error.
   - On Windows, stage new check files with `git add --chmod=+x` (or run
     `bin/mil link` after `git add` so the git index gets mode `100755`).
4. **Prove and lint**:
   Run `bin/mil prove F-... --check-hunks` and `bin/mil lint`.
   - Ordering rule: `record` -> `scaffold` + edit check (or write check + `link`) -> `prove` -> `lint`.
   - On success, `mil prove` advances `open -> proven` (locking the check against further silent edits).
5. **Ask before enabling**:
   Ask the user whether to enable `<check-id>` (`F-...`) in `shadow`
   mode (reports hits in CI/hooks without blocking or commenting on PRs)
   or keep it `proven`. On an explicit yes, print
   `bin/mil promote F-... --approved-by @their-handle` for the user to
   run in their own terminal and type `shadow` (`mil promote --dry-run`
   may be run to preview, and `block` requires the check to be green on
   the product tree first).
