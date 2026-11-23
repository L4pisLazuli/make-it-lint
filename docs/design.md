# Design (`make-it-lint/v1`)

`make-it-lint` turns concrete mistakes made by AI coding agents into
executable checks backed by reproducible `bad/` and `good/` fixtures.
Everything needed to record, classify, prove, guard, and roll out those
checks lives in this repository—no external harness required.

## Core principles

1. **No prompt-only fixes.** A prose warning in `AGENTS.md` decays; an
   executable check with a red/green fixture pair does not.
2. **Stop at the earliest layer that can see the mistake.**
   `architecture -> type -> lint -> test -> verify -> hook -> ci`.
   Prose documentation (`doc`) is intentionally excluded as a stop layer:
   if no mechanical layer in the repository can observe the mistake, the
   record stays `open` with `layer: null` and a clear `layer_reason` (or
   moves to `human_review` when `decidable: "no"`).
3. **Every check is proven before rollout.** `mil prove` requires exit 1
   with structured `<path>[:<line>]: <message>` output on every `bad/`
   fixture and exit 0 on every `good/` fixture. `mil prove --check-hunks`
   reverts fix hunks from `good/` toward `bad/` and warns if the check
   stays green.
4. **Human-gated rollout and reversibility.** `mil prove` moves `open -> proven`.
   Moving `proven -> shadow -> comment -> block` requires a person at an
   interactive terminal to run `mil promote --approved-by <who>` and type
   the target state (agent runtimes are refused, and moving to `block`
   also requires the check to be green on the current product tree). Noisy
   checks step down cleanly via `mil demote`, and obsolete checks retire
   via `mil retire`.
5. **Observation belongs in events, not manual YAML counters.** Ledger
   records stay immutable except for state transitions and occurrences;
   runtime hit/run counts come from `events.ndjson` via
   `mil events --summary`.

## Lifecycle

```
open ──(mil prove)──▶ proven ──(mil promote)──▶ shadow ──▶ comment ──▶ block
 │  ▲                   ▲                         │          │          │
 │  └──(mil classify)───┼─────────────────────────┴──────────┴──────────┘
 ▼                      │                 (mil demote, one step)
human_review            └─── (any state ──(mil retire)──▶ retired)
```

## Check inputs

- `input: tree` — inspects a directory snapshot (`--root`).
- `input: diff` — compares two snapshots (`--base` and `--head`), with
  clean git materialization via `mil ci --staged` or
  `mil ci --base-ref <ref> --head-ref HEAD`.
- `input: action` — inspects a normalized tool-call JSON payload
  (`--action`) inside `mil guard` (`PreToolUse` hook) so destructive
  shell commands or edits are stopped before execution.

## Repository layout

- `bin/mil`, `bin/mil.cmd` — CLI (`record`, `classify`, `scaffold`, `link`,
  `prove`, `promote`, `demote`, `retire`, `status`, `lint`, `ci`, `guard`,
  `events`)
- `bin/mil-pre-commit` — staged index enforcement (`mil ci --staged`)
- `bin/mil-setup`, `bin/mil-setup.cmd` — cross-platform idempotent
  installer/upgrader for adopter repos (`lib/mil/setup_repo.py`)
- `lib/mil/` — stdlib-only Python 3.10+ implementation
- `schema/` — JSON Schemas (`config`, `incident`, `check-meta`, `waiver`)
- `skills/make-it-lint/` — Agent Skill and hooks for Claude Code & Cursor
- `template/` — adopter skeleton, `.gitattributes`, `.gitignore`, and
  GitHub Actions workflow + PR comment script
- `examples/sample-repo/` — seven anonymized records and six checks
  covering `tree`, `diff`, `action`, and `verify` (`MIL_SEED`)
- `.make-it-lint/` — ledger, checks, config, and waivers for this repository
  (empty until a failure is recorded here)
