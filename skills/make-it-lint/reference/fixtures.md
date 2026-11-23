# `bad/` and `good/` fixtures

A check is not proven until `mil prove` exits `1` (with at least one
`<path>[:<line>]: <message>` line) on every `bad/` variant and exits `0`
on every `good/` variant. Empty fixtures are rejected.

## Layout

Single fixture pair:

```
ledger/F-YYYYMMDD-NN/
  record.yaml
  bad/
  good/
```

Multi-variant fixtures (every named subdirectory under `bad/` and `good/`
is executed):

```
ledger/F-YYYYMMDD-NN/
  record.yaml
  bad/
    real/            # required when bad/ has multiple variants
    near-drop-first/ # any descriptive variant directory name
  good/
    keep-all/
    reorder/
```

 Do not leave stray files directly under `bad/` or `good/` when using
named variant subdirectories; `mil lint` and `mil prove` reject them so
nothing is silently skipped.

## By check `input`

- **`input: tree`** — each variant directory is passed as `--root`.
- **`input: diff`** — each variant directory must contain `base/` and
  `head/`. An optional `pr.yaml` (`kind`, `branch`, `labels`) in the
  variant or `head/` populates `MIL_PR_KIND`, `MIL_PR_BRANCH`, and
  `MIL_PR_LABELS` without leaking outer environment variables.
- **`input: action`** — each variant directory must contain `action.json`
  with the normalized tool-call shape (`tool`, `kind`, `command`, `path`,
  `paths`, `tool_input`), passed as `--action <variant>/action.json`.

Origin checks (metadata `origin`, no `incident`) may bundle `bad/` and
`good/` next to `check.*` inside `.make-it-lint/checks/<dir>/`.

## Realistic fixture data

Always mirror the actual domain structure of the failure (anonymized
where needed): real config keys, real file layouts, real boundary cases—
never meaningless placeholders.

## Hunk-revert check (`--check-hunks`)

`mil prove --check-hunks` reverts one non-comment fix hunk at a time from
`good/` back toward `bad/` and warns if the check stays green, catching
overly narrow rules that only match a single line of a multi-part bug.
