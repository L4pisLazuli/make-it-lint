# Failure kind -> layer and tool

`mil record --kind <k>` and `mil classify F-... --kind <k>` print a
one-line routing suggestion from `config.yaml` `ledger.kind_defaults`:

| Failure shape | Suggested `layer` / `tool` | Default `input` | Example `kind` |
|---|---|---|---|
| Single-file syntax or AST pattern | `lint` / `script` (or `semgrep`, `ast-grep`) | `tree` | — |
| Multi-file diff, generated files, PR path scope | `ci` / `script` | `diff` | `scope` |
| Destructive command or edit during tool call | `hook` / `hook` | `action` | `boot` |
| Output shape, golden file, runner exit code | `test` / `test` (or `script`) | `tree` or `diff` | `reporting` |
| Invariant across arbitrary inputs / round-trip | `verify` / `property` | `tree` | `data` |
| Ordering, concurrency, state machine | `verify` / `model` | `tree` | `freeze` |
| Mechanically undecidable trade-off | `human_review` (no check) | — | `judgment` |

Unregistered `kind` values emit a lint warning rather than an error so
teams can record a failure immediately without editing `config.yaml` first.

## Why `kind=scope` defaults to `ci` rather than `lint`

- `lint` inspects a single file or tree snapshot on disk without git
  history. When the bad state is visible in the tree alone (for example
  a forbidden file or debug call), pass `--layer lint` explicitly with a
  `--layer-reason`; an explicit `--layer` always wins and suppresses the
  kind-based suggestion.
- `scope` failures ("a collect PR also modified `config/runner.yaml`")
  depend on what a change touched (`base` vs `head`) and on PR metadata
  (`MIL_PR_BRANCH` / `MIL_PR_LABELS`), so they use `input: diff` and run
  at `ci` (and in `mil ci --staged` during pre-commit).
