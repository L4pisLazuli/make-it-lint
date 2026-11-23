# make-it-lint/v1

Version: `make-it-lint/v1`

This document specifies the self-contained `make-it-lint` contract: how
ledger records, checks, waivers, configuration, CI runs, PreToolUse
action guards, and event logs fit together inside a repository.

The JSON Schemas named here live in `schema/` (the single source of
truth loaded directly by `mil lint` and the test suite).

## 1. Ledger records and waivers

- **Record schema**: `schema/incident.schema.json`.
- **Location**: `.make-it-lint/ledger/F-YYYYMMDD-NN/`, containing
  `record.yaml` and, once a check is attached, `bad/` and `good/`.
  Multi-variant fixtures place named subdirectories under `bad/` and
  `good/` (`bad/real/` is required whenever `bad/` uses multiple
  variants; every named subdirectory under `bad/` and `good/` is run).
- **Lifecycle states**:
  - `open` — recorded failure (classified or awaiting `mil classify`),
    not yet proven.
  - `proven` — `mil prove` verified exit 1 on every `bad/` variant and
    exit 0 on every `good/` variant.
  - `shadow` — runs on product code / actions; reports hits in logs and
    events, never comments on PRs, never blocks.
  - `comment` — emits GitHub warnings / PR comments or hook context
    notes; never fails CI or blocks tool calls.
  - `block` — fails CI / pre-commit / `PreToolUse` unless covered by an
    unexpired waiver.
  - `human_review` — mechanically undecidable (`decidable: "no"`);
    surfaced in status and CI without a check.
  - `retired` — structural root cause removed; no longer run in CI or
    hooks, while keeping the historical record and fixtures intact.
- **Allowed state transitions**:
  - `open -> proven` (via `mil prove`), `open -> human_review` (via
    `mil classify`), `human_review -> open` (via `mil classify`).
  - `proven -> shadow -> comment -> block` one step at a time via
    `mil promote --approved-by <human>`, run by a person at a terminal
    who types the target state (`mil promote` refuses agent runtimes and
    non-TTY stdin, and refuses `block` when the check is red on the
    product tree; history records `from_state`, `state`, `at`, and
    `approved_by`).
  - `block -> comment -> shadow -> proven` one step at a time via
    `mil demote --reason <why>`.
  - Any non-retired state `-> retired` via `mil retire --reason <why>`.
- **Optional record attachments**:
  - `evidence`: list of `{kind?, path + sha256 | url, note?}`. Every
    `path` must stay inside the repository root and match `sha256`.
  - `covers`: `{files, synced_commit}`. `mil lint` warns when tracked
    files changed after `synced_commit`.
- **Waivers**: `.make-it-lint/waivers.yaml` (`schema/waiver.schema.json`).
  Each entry names `check`, `paths` (exact paths, directory prefixes, or
  globs), `reason`, human `approved_by`, and `expires` (`YYYY-MM-DD`).
  An unexpired waiver downgrades `block` to `comment` on matching paths.

## 2. Checks and metadata

- **Location**: `.make-it-lint/checks/`. A check is an executable file or
  a subdirectory containing a single `check.*` executable. Helper files
  starting with `_` or `.` are ignored during discovery.
- **Leading metadata**: YAML between `# ---` (or `// ---`) fences at the
  top of the executable (`schema/check-meta.schema.json`):
  - `id`: check identifier (`MIL-...` or `[A-Za-z0-9][A-Za-z0-9_.-]*`).
  - Either `incident: F-YYYYMMDD-NN` (state comes from `record.yaml`;
    metadata must not duplicate `enforcement`) or `origin: <source>`
    with `enforcement.state` (`proven | shadow | comment | block | retired`).
  - `input`: `tree` (default), `diff`, or `action`.
  - Optional `layer` (`architecture | type | lint | test | verify | hook | ci`)
    and `tool` (`script | semgrep | ast-grep | test | hook | property | runtime | model`),
    which must match `record.yaml` when both are present.
- **Invocation**:
  - `input: tree`: `<check> --root <dir> --config <config.yaml>`
  - `input: diff`: `<check> --base <dir> --head <dir> --config <config.yaml>`
    (prove fixtures contain `base/` and `head/`; optional `pr.yaml` in
    the fixture or `head/` sets `MIL_PR_BRANCH`, `MIL_PR_LABELS`, and
    `MIL_PR_KIND` without leaking caller environment variables).
  - `input: action`: `<check> --action <action.json> --root <dir> --config <config.yaml>`
    (prove fixtures contain `action.json`; `mil guard` runs active
    `action` checks during `PreToolUse`).
  - Environment exported to every check:
    - `MIL_SEED`: integer from `proof.seed` (or metadata `seed`, default `0`).
    - `MIL_CHECK_CONFIG_JSON`: JSON object for `config.yaml` `checks.<id>`
      so checks in any language or Python stdlib can read their settings
      without a YAML parser.
    - `MIL_CONFIG_JSON`: full `config.yaml` serialized as JSON.
  - `layer: verify` checks are executed twice per invocation; differing
    exit code or stdout fails with reproducibility error (exit 2).
- **Exit codes and stdout**:
  - `0` = no hit, `1` = one or more hits, `2` = check error.
  - Every hit on stdout must match `<path>[:<line>]: <message>`.

## 3. Enforcement, events, and PR comments

- `mil ci` runs product checks (`input: tree` and `input: diff`) in
  `shadow`, `comment`, or `block` state. `--staged` materializes `HEAD`
  and the git index; `--base-ref` and `--head-ref` materialize clean git
  trees without `git worktree` or `tar`.
- Hits under `.make-it-lint/ledger/**/bad/` or `good/` are never counted
  as product hits.
- Events (`--events FILE` or `config.yaml` `events.commit: true` ->
  `.make-it-lint/events.ndjson`) write one UTC JSON line per event:
  `type` in `shadow_run | shadow_hit | comment_hit | block_hit | waiver | check_error | prove`.
  `mil events --summary` aggregates shadow runs, hits, waivers, errors,
  and proofs per check from one or more files/directories so humans can
  decide when to run `mil promote`.
- `.github/scripts/pr-comment.py` posts a PR comment only when there are
  `comment` hits, `block` hits, check errors, or governance/protected
  changes (or updates an existing `<!-- mil-ci -->` comment when prior
  hits are resolved). Shadow-only runs stay silent on PRs.

## 4. Protected paths and hooks (`mil guard`)

- Protected governance paths:
  - `.make-it-lint/waivers.yaml` (always human-only).
  - Existing `.make-it-lint/ledger/F-.../record.yaml` files (managed via
    `mil` CLI commands rather than direct file edits; rewriting or
    truncating `history` or changing `id`/`fingerprint` fails `mil guard`
    and `mil ci`).
  - Existing files under `.make-it-lint/checks/` once proven or committed
    to git `HEAD` (new check files and uncommitted draft checks whose
    incident record is still `open` are allowed; editing a proven or
    committed check or deleting any check requires human review).
  - Kit-owned files in adopter repositories (`bin/mil`,
    `bin/mil-pre-commit`, `lib/mil/**`, `schema/**`, `docs/contract.md`;
    refreshed via `bin/mil-setup`).
  - Custom glob/prefix patterns listed under `protected_paths` in
    `.make-it-lint/config.yaml`.
- `mil-setup --hooks` installs:
  - `.git/hooks/pre-commit` -> `bin/mil-pre-commit` (`mil ci --staged`).
  - Claude Code (`.claude/settings.json`) and Cursor (`.cursor/hooks.json`)
    hooks:
    - `PreToolUse` / `preToolUse`: `hooks/pre_tool_guard.py` (`mil guard --hook`),
      blocking direct agent writes to protected paths and running active
      `input: action` checks.
    - `PostToolUse` / `PostToolUseFailure`: `hooks/session_log.py`.
    - `Stop` / `stop`: `hooks/stop-suggest.sh` (`hooks/stop_suggest.py`),
      suggesting `mil record` when a failing command or test/lint runner
      later passes in the same session.
