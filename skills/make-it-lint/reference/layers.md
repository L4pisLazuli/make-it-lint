# Choosing a stop layer

Pick the earliest mechanical layer that can observe the failure and
record the reason in `layer_reason`:

1. **`architecture`** — module boundaries, constructors, or schemas make
   the invalid state unrepresentable.
2. **`type`** — static type-checking rejects the invalid program.
3. **`lint`** — a single file on disk is enough (`input: tree`).
4. **`test`** — unit/integration test, fixture, baseline, or suite
   runner invariant.
5. **`verify`** — seeded property check (`tool: property`), state/trace
   model (`tool: model`), or runtime invariant (`tool: runtime`).
   `mil prove` exports `MIL_SEED` from `proof.seed` (default `0`) and
   runs the check twice to enforce deterministic output.
6. **`hook`** — the mistake is a tool action (`Edit`, `Write`, `Bash`,
   `git push --force`, etc.) that must be intercepted before execution
   via `mil guard` (`PreToolUse`, `input: action`).
7. **`ci`** — requires comparing `base/` and `head/` across a pull
   request or knowing the PR kind/labels (`input: diff`).

Prose documentation is **not** a stop layer in `make-it-lint/v1`. When
the root cause lives outside the repository (e.g. an external hosted UI
default), keep the record `open` with `--no-layer` (`layer: null`) and
explain why in `layer_reason`.

On a repeated occurrence of the same `--fingerprint`, propose moving one
layer earlier in this list rather than adding more prose.
