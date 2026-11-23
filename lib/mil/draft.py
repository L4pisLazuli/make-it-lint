"""Automatic stop-layer / tool selection and provisional check assembly (provisional draft).

Inspects `bad/` and `good/` fixtures alongside the repository toolchain to:
1. Recommend (or apply with `--auto`) the earliest mechanical stop layer
   (`architecture -> type -> lint -> test -> verify -> hook -> ci`) and tool.
2. Assemble a provisional check from structural differences between `bad/` and
   `good/` (Python `ast` / `tokenize`, JSON/YAML config keys, diff path scope
   and co-change rules, `PreToolUse` action guards, or `semgrep` / `ast-grep`
   rule bundles).
3. Self-verify any synthesized provisional rule against all `bad/` and `good/`
   variants before keeping it, falling back to an annotated `exit 2` scaffold
   whenever static synthesis cannot provably separate `bad/` from `good/`.
"""

from __future__ import annotations

import ast
import io
import json
import re
import shlex
import shutil
import subprocess
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mil import yamlio
from mil.checks import (
    CheckError,
    NonReproducibleError,
    extract_metadata,
    invoke_check_reproducible,
    resolve_check_program,
)
from mil.fixtures import is_multi_fixture, list_bad_variants, list_good_variants
from mil.models import HIT_RE, proven_result_text
from mil.paths import as_rel, config_path

DEBUG_MODULES = frozenset({"pdb", "ipdb", "pudb", "bdb", "debugpy", "icecream", "devtools"})
HAZARD_CALLS_PRIORITY = (
    "breakpoint",
    "pdb.set_trace",
    "ipdb.set_trace",
    "eval",
    "exec",
    "os.system",
    "os.popen",
    "pickle.loads",
    "yaml.unsafe_load",
    "print",
    "time.sleep",
)
HAZARD_KWARGS_PRIORITY = (
    ("shell", "True"),
    ("verify", "False"),
    ("check", "False"),
)
CODE_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".rb", ".sh"})
CONFIG_SUFFIXES = frozenset({".json", ".yaml", ".yml"})


@dataclass(frozen=True)
class RepoToolchain:
    ast_grep_bin: str | None = None
    semgrep_bin: str | None = None
    has_ast_grep_config: bool = False
    has_semgrep_config: bool = False
    has_ruff: bool = False
    has_mypy_or_pyright: bool = False
    has_eslint_or_biome: bool = False
    has_tsconfig: bool = False

    def available_external_linters(self) -> list[str]:
        out: list[str] = []
        if self.ast_grep_bin:
            out.append("ast-grep")
        if self.semgrep_bin:
            out.append("semgrep")
        return out


@dataclass(frozen=True)
class ClassificationPlan:
    kind: str
    decidable: str
    layer: str
    tool: str
    check_input: str
    layer_reason: str
    strategy: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DraftBundle:
    stored_rel: str
    program_rel: str
    files: dict[str, str]
    is_provisional_rule: bool
    strategy: str


def _probe_version(binary: str, expected_substring: str | None = None) -> bool:
    try:
        proc = subprocess.run(
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    if expected_substring:
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
        return expected_substring.lower() in combined
    return True


def detect_toolchain(root: Path) -> RepoToolchain:
    ast_grep = shutil.which("ast-grep")
    if ast_grep and not _probe_version(ast_grep, "ast-grep"):
        ast_grep = None
    if not ast_grep:
        sg = shutil.which("sg")
        # On Linux, /usr/bin/sg is shadow-utils group switcher, not ast-grep.
        if sg and _probe_version(sg, "ast-grep"):
            ast_grep = sg

    semgrep = shutil.which("semgrep")
    if semgrep and not _probe_version(semgrep):
        semgrep = None

    pyproject_text = ""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace")

    has_ast_grep_cfg = (root / "sgconfig.yml").is_file() or (root / "sgconfig.yaml").is_file()
    has_semgrep_cfg = (
        (root / ".semgrep.yml").is_file()
        or (root / ".semgrep.yaml").is_file()
        or (root / ".semgrep").is_dir()
    )
    has_ruff = (
        (root / "ruff.toml").is_file()
        or (root / ".ruff.toml").is_file()
        or "[tool.ruff" in pyproject_text
    )
    has_mypy_pyright = (
        (root / "mypy.ini").is_file()
        or (root / ".mypy.ini").is_file()
        or (root / "pyrightconfig.json").is_file()
        or "[tool.mypy" in pyproject_text
        or "[tool.pyright" in pyproject_text
    )
    has_eslint_biome = any(
        (root / name).is_file()
        for name in (
            "biome.json",
            "biome.jsonc",
            "eslint.config.js",
            "eslint.config.mjs",
            ".eslintrc",
            ".eslintrc.json",
            ".eslintrc.yml",
            ".eslintrc.js",
        )
    )
    return RepoToolchain(
        ast_grep_bin=ast_grep,
        semgrep_bin=semgrep,
        has_ast_grep_config=has_ast_grep_cfg,
        has_semgrep_config=has_semgrep_cfg,
        has_ruff=has_ruff,
        has_mypy_or_pyright=has_mypy_pyright,
        has_eslint_or_biome=has_eslint_biome,
        has_tsconfig=(root / "tsconfig.json").is_file(),
    )


SKIP_TREE_PARTS = frozenset(
    {
        ".git",
        ".make-it-lint",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gitkeep",
    }
)


def _tree_files(tree: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not tree.is_dir():
        return out
    for p in sorted(tree.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(tree).parts
        if any(part in SKIP_TREE_PARTS for part in rel_parts):
            continue
        rel = p.relative_to(tree).as_posix()
        if rel == "pr.yaml":
            continue
        out[rel] = p
    return out


def _summarize_tree_diff(bad_dirs: list[Path], good_dirs: list[Path]) -> str:
    changed: set[str] = set()
    b0 = _tree_files(bad_dirs[0]) if bad_dirs else {}
    g0 = _tree_files(good_dirs[0]) if good_dirs else {}
    for rel in sorted(set(b0) | set(g0)):
        bp = b0.get(rel)
        gp = g0.get(rel)
        if bp is None or gp is None or bp.read_bytes() != gp.read_bytes():
            changed.add(rel)
    if not changed:
        return "no file differences detected between bad/ and good/"
    shown = ", ".join(sorted(changed)[:5])
    return f"differed in {shown}" + (" ..." if len(changed) > 5 else "")


def infer_plan(
    root: Path,
    bad_dir: Path,
    good_dir: Path,
    *,
    toolchain: RepoToolchain | None = None,
    explicit_layer: str | None = None,
    explicit_tool: str | None = None,
    explicit_input: str | None = None,
) -> ClassificationPlan:
    tc = toolchain or detect_toolchain(root)
    multi = is_multi_fixture(bad_dir, good_dir) if (bad_dir.is_dir() and good_dir.is_dir()) else False
    bad_vars = [d for _, d in list_bad_variants(bad_dir, multi=multi)] if bad_dir.is_dir() else []
    good_vars = [d for _, d in list_good_variants(good_dir, multi=multi)] if good_dir.is_dir() else []

    bad_nonempty = [d for d in bad_vars if d.is_dir() and any(c for c in d.iterdir() if not c.name.startswith("."))]
    good_nonempty = [d for d in good_vars if d.is_dir() and any(c for c in d.iterdir() if not c.name.startswith("."))]

    if not bad_nonempty or not good_nonempty:
        layer = explicit_layer or "lint"
        c_input = explicit_input or ("action" if layer == "hook" else ("diff" if layer == "ci" else "tree"))
        tool = explicit_tool or ("hook" if layer == "hook" else ("property" if layer == "verify" else ("test" if layer == "test" else "script")))
        return ClassificationPlan(
            kind=layer if layer in ("architecture", "type", "lint", "test") else ("boot" if layer == "hook" else ("data" if layer == "verify" else "scope")),
            decidable="yes",
            layer=layer,
            tool=tool,
            check_input=c_input,
            layer_reason=f"Selected {layer} layer ({c_input} input).",
            strategy="manual_template",
            details={"summary": "fixtures are empty"},
        )

    # 1. Action fixtures (`action.json`)
    if explicit_input == "action" or all((d / "action.json").is_file() for d in bad_nonempty + good_nonempty):
        plan = _analyze_action_fixtures(bad_nonempty, good_nonempty)
        return _apply_overrides(plan, explicit_layer, explicit_tool, explicit_input)

    # 2. Diff fixtures (`base/` and `head/`)
    if explicit_input == "diff" or all((d / "base").is_dir() and (d / "head").is_dir() for d in bad_nonempty + good_nonempty):
        plan = _analyze_diff_fixtures(bad_nonempty, good_nonempty)
        return _apply_overrides(plan, explicit_layer, explicit_tool, explicit_input)

    # 3. Tree fixtures — earliest stop layer cascade: architecture -> type -> lint -> test -> verify
    plan = _analyze_tree_fixtures(bad_nonempty, good_nonempty, multi=multi, toolchain=tc, explicit_tool=explicit_tool)
    return _apply_overrides(plan, explicit_layer, explicit_tool, explicit_input)


def _apply_overrides(
    plan: ClassificationPlan,
    explicit_layer: str | None,
    explicit_tool: str | None,
    explicit_input: str | None,
) -> ClassificationPlan:
    layer = explicit_layer or plan.layer
    c_input = explicit_input or plan.check_input
    tool = explicit_tool or plan.tool
    strategy = plan.strategy
    if explicit_input and explicit_input != plan.check_input:
        strategy = "manual_template"
    if tool in ("semgrep", "ast-grep") and strategy not in ("semgrep_bundle", "ast_grep_bundle"):
        strategy = "semgrep_bundle" if tool == "semgrep" else "ast_grep_bundle"
    return ClassificationPlan(
        kind=plan.kind,
        decidable=plan.decidable,
        layer=layer,
        tool=tool,
        check_input=c_input,
        layer_reason=plan.layer_reason,
        strategy=strategy,
        details=dict(plan.details),
    )


# ---------------------------------------------------------------------------
# Action fixture analysis (`input: action`)
# ---------------------------------------------------------------------------


def _analyze_action_fixtures(bad_dirs: list[Path], good_dirs: list[Path]) -> ClassificationPlan:
    try:
        b_acts = [json.loads((d / "action.json").read_text(encoding="utf-8")) for d in bad_dirs]
        g_acts = [json.loads((d / "action.json").read_text(encoding="utf-8")) for d in good_dirs]
    except (OSError, ValueError):
        return ClassificationPlan(
            kind="boot",
            decidable="yes",
            layer="hook",
            tool="hook",
            check_input="action",
            layer_reason="Tool action payload in action.json is intercepted in PreToolUse.",
            strategy="manual_template",
        )

    b_kinds = {str(a.get("kind") or "") for a in b_acts if isinstance(a, dict)}
    if b_kinds == {"command"}:
        b_tokens = [_split_cmd(str(a.get("command") or "")) for a in b_acts]
        g_tokens = [_split_cmd(str(a.get("command") or "")) for a in g_acts if isinstance(a, dict)]
        if all(b_tokens):
            prefix = _common_prefix(b_tokens)
            b_sets = [set(toks[len(prefix) :]) for toks in b_tokens]
            common_extra = set.intersection(*b_sets) if b_sets else set()
            g_matching_sets = [
                set(toks[len(prefix) :])
                for toks in g_tokens
                if len(toks) >= len(prefix) and toks[: len(prefix)] == prefix
            ]
            # Pick minimal discriminating token(s) present in every bad command with this prefix
            discriminators = sorted(
                tok for tok in common_extra if all(tok not in gs for gs in g_matching_sets)
            )
            if prefix and (discriminators or not g_matching_sets):
                chosen = discriminators[:2] if discriminators else []
                desc = " ".join([*prefix, *chosen])
                return ClassificationPlan(
                    kind="boot",
                    decidable="yes",
                    layer="hook",
                    tool="hook",
                    check_input="action",
                    layer_reason=f"Destructive command `{desc}` can be blocked in PreToolUse before execution.",
                    strategy="action_command",
                    details={"prefix": prefix, "required_tokens": chosen},
                )

    if b_kinds and b_kinds <= {"write", "delete"}:
        b_paths = [
            set(a.get("paths") or ([a["path"]] if a.get("path") else []))
            for a in b_acts
            if isinstance(a, dict)
        ]
        g_paths_union: set[str] = set()
        for a in g_acts:
            if isinstance(a, dict):
                g_paths_union.update(a.get("paths") or ([a["path"]] if a.get("path") else []))
        common_bad_paths = set.intersection(*b_paths) - g_paths_union if b_paths else set()
        if common_bad_paths:
            forbidden = sorted(common_bad_paths)
            return ClassificationPlan(
                kind="boot",
                decidable="yes",
                layer="hook",
                tool="hook",
                check_input="action",
                layer_reason=f"Protected path write/delete ({forbidden[0]}) can be blocked in PreToolUse.",
                strategy="action_path",
                details={"kinds": sorted(b_kinds), "forbidden_paths": forbidden},
            )

    return ClassificationPlan(
        kind="boot",
        decidable="yes",
        layer="hook",
        tool="hook",
        check_input="action",
        layer_reason="Tool action payload in action.json is intercepted in PreToolUse.",
        strategy="manual_template",
    )


def _split_cmd(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()


def _common_prefix(lists: list[list[str]]) -> list[str]:
    if not lists:
        return []
    prefix: list[str] = []
    for items in zip(*lists):
        if len(set(items)) == 1 and not items[0].startswith("-"):
            prefix.append(items[0])
        else:
            break
    return prefix[:2]


# ---------------------------------------------------------------------------
# Diff fixture analysis (`input: diff`)
# ---------------------------------------------------------------------------


def _changed_between(base: Path, head: Path) -> set[str]:
    bf = _tree_files(base)
    hf = _tree_files(head)
    out: set[str] = set()
    for rel in set(bf) | set(hf):
        bp = bf.get(rel)
        hp = hf.get(rel)
        if bp is None or hp is None or bp.read_bytes() != hp.read_bytes():
            out.add(rel)
    return out


def _read_pr_meta(variant_dir: Path) -> dict[str, Any]:
    for p in (variant_dir / "pr.yaml", variant_dir / "head" / "pr.yaml"):
        if p.is_file():
            try:
                loaded = yamlio.load_path(str(p))
                if isinstance(loaded, dict):
                    return loaded
            except yamlio.YAMLError:
                pass
    return {}


def _analyze_diff_fixtures(bad_dirs: list[Path], good_dirs: list[Path]) -> ClassificationPlan:
    b_changed = [_changed_between(d / "base", d / "head") for d in bad_dirs]
    g_changed = [_changed_between(d / "base", d / "head") for d in good_dirs]
    b_pr = [_read_pr_meta(d) for d in bad_dirs]

    common_bad_changed = set.intersection(*b_changed) if b_changed else set()
    union_good_changed = set.union(*g_changed) if g_changed else set()

    # 2A-1: Out-of-scope path touched in bad, never touched in good
    bad_only_paths = sorted(common_bad_changed - union_good_changed)
    if bad_only_paths:
        good_top_dirs = sorted({p.split("/")[0] + "/" for p in union_good_changed if "/" in p})
        bad_top_dirs = {p.split("/")[0] + "/" for p in bad_only_paths if "/" in p}
        pr_kinds = {str(m.get("kind") or "") for m in b_pr if m.get("kind")}
        pr_branches = [str(m.get("branch") or "") for m in b_pr if m.get("branch")]
        branch_prefix = ""
        if pr_branches and all("/" in b for b in pr_branches):
            prefixes = {b.split("/", 1)[0] + "/" for b in pr_branches}
            if len(prefixes) == 1:
                branch_prefix = next(iter(prefixes))
        pr_kind = next(iter(pr_kinds)) if len(pr_kinds) == 1 else ""

        if good_top_dirs and not (set(good_top_dirs) & bad_top_dirs):
            return ClassificationPlan(
                kind="scope",
                decidable="yes",
                layer="ci",
                tool="script",
                check_input="diff",
                layer_reason="Requires comparing base/ and head/ changed paths (and PR scope metadata) in CI / pre-commit.",
                strategy="diff_allowed_prefixes",
                details={
                    "allowed_prefixes": good_top_dirs,
                    "pr_kind": pr_kind,
                    "branch_prefix": branch_prefix,
                },
            )
        return ClassificationPlan(
            kind="scope",
            decidable="yes",
            layer="ci",
            tool="script",
            check_input="diff",
            layer_reason="Requires comparing base/ and head/ changed paths across a change.",
            strategy="diff_forbidden_paths",
            details={
                "forbidden_paths": bad_only_paths,
                "pr_kind": pr_kind,
                "branch_prefix": branch_prefix,
            },
        )

    # 2A-2: Missing co-change (good changes {trigger, companion} together; bad changes trigger without companion)
    common_good_changed = set.intersection(*g_changed) if g_changed else set()
    missing_companions = sorted(common_good_changed - set.union(*b_changed)) if b_changed else []
    triggers = sorted(common_bad_changed & common_good_changed)
    if triggers and missing_companions:
        return ClassificationPlan(
            kind="scope",
            decidable="yes",
            layer="ci",
            tool="script",
            check_input="diff",
            layer_reason=f"Requires verifying that changes to {triggers[0]} also update {missing_companions[0]} between base/ and head/.",
            strategy="diff_cochange",
            details={"triggers": triggers, "companions": missing_companions},
        )

    return ClassificationPlan(
        kind="scope",
        decidable="yes",
        layer="ci",
        tool="script",
        check_input="diff",
        layer_reason="Requires comparing base/ and head/ trees across a change.",
        strategy="manual_template",
        details={"summary": f"bad touched {sorted(common_bad_changed)}"},
    )


# ---------------------------------------------------------------------------
# Tree fixture analysis (`input: tree`)
# ---------------------------------------------------------------------------


def _dotted_expr(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_expr(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


@dataclass
class _PyFeatures:
    imports: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)
    call_kwargs: set[tuple[str, str, str]] = field(default_factory=set)
    local_defs: set[str] = field(default_factory=set)
    has_bare_except: bool = False
    has_pass_except: bool = False
    has_mutable_default: bool = False
    has_type_ignore: bool = False
    func_names_by_file: dict[str, list[str]] = field(default_factory=dict)


def _extract_py_features(tree_dir: Path) -> _PyFeatures | None:
    files = {rel: p for rel, p in _tree_files(tree_dir).items() if p.suffix == ".py"}
    if not files:
        return None
    feats = _PyFeatures()
    for rel, path in files.items():
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            mod = ast.parse(src, filename=rel)
        except SyntaxError:
            continue

        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT and re.search(r"#\s*type:\s*ignore\b", tok.string):
                    feats.has_type_ignore = True
        except tokenize.TokenError:
            pass

        funcs: list[str] = []
        for node in ast.walk(mod):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                feats.local_defs.add(node.name)
                funcs.append(node.name)
                defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
                if any(isinstance(d, (ast.List, ast.Dict, ast.Set)) for d in defaults):
                    feats.has_mutable_default = True
            elif isinstance(node, ast.ClassDef):
                feats.local_defs.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    feats.imports.add(alias.name)
                    feats.imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    feats.imports.add(node.module)
                    feats.imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                cname = _dotted_expr(node.func)
                if cname:
                    feats.calls.add(cname)
                    for kw in node.keywords:
                        if kw.arg and isinstance(kw.value, ast.Constant) and isinstance(
                            kw.value.value, (bool, int, str, type(None))
                        ):
                            feats.call_kwargs.add((cname, kw.arg, repr(kw.value.value)))
            elif isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    feats.has_bare_except = True
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    feats.has_pass_except = True
        if funcs:
            feats.func_names_by_file[rel] = funcs
    return feats


def _flatten_config_scalars(data: Any, prefix: str = "") -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    if isinstance(data, dict):
        for k, v in data.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten_config_scalars(v, key))
    elif isinstance(data, (bool, int, float, str)) or data is None:
        if prefix:
            out.add((prefix, json.dumps(data, ensure_ascii=False)))
    return out


def _extract_config_features(tree_dir: Path) -> tuple[set[str], set[tuple[str, str]]]:
    cfg_files = {rel: p for rel, p in _tree_files(tree_dir).items() if p.suffix.lower() in CONFIG_SUFFIXES}
    kv: set[tuple[str, str]] = set()
    for path in cfg_files.values():
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            data = json.loads(text) if path.suffix.lower() == ".json" else yamlio.load(text)
        except (ValueError, yamlio.YAMLError):
            continue
        if isinstance(data, dict):
            kv.update(_flatten_config_scalars(data))
    return set(cfg_files), kv


def _analyze_tree_fixtures(
    bad_dirs: list[Path],
    good_dirs: list[Path],
    *,
    multi: bool,
    toolchain: RepoToolchain,
    explicit_tool: str | None,
) -> ClassificationPlan:
    b_file_maps = [_tree_files(d) for d in bad_dirs]
    g_file_maps = [_tree_files(d) for d in good_dirs]

    common_bad_rels = set.intersection(*(set(m) for m in b_file_maps)) if b_file_maps else set()
    union_good_rels = set.union(*(set(m) for m in g_file_maps)) if g_file_maps else set()

    # 3.1 Forbidden extra file(s) present in every bad tree and absent from every good tree
    extra_rels = sorted(common_bad_rels - union_good_rels)
    if extra_rels:
        # Check that overlapping files between bad[0] and good[0] are unchanged
        shared = set(b_file_maps[0]) & set(g_file_maps[0])
        if all(b_file_maps[0][r].read_bytes() == g_file_maps[0][r].read_bytes() for r in shared):
            return ClassificationPlan(
                kind="lint",
                decidable="yes",
                layer="lint",
                tool="script",
                check_input="tree",
                layer_reason=f"Forbidden file `{extra_rels[0]}` is directly observable in a single tree snapshot.",
                strategy="tree_forbidden_path",
                details={"forbidden_paths": extra_rels},
            )

    # 3.2 Python AST & Tokenize inspection (`architecture -> type -> lint`)
    b_py = [_extract_py_features(d) for d in bad_dirs]
    g_py = [_extract_py_features(d) for d in good_dirs]
    if all(f is not None for f in b_py) and all(f is not None for f in g_py):
        b_feats = [f for f in b_py if f is not None]
        g_feats = [f for f in g_py if f is not None]

        bad_only_imports = set.intersection(*(f.imports for f in b_feats)) - set.union(
            *(f.imports for f in g_feats)
        )
        if bad_only_imports:
            # Prefer full dotted import names over root package duplicates
            chosen_imps = sorted(
                imp
                for imp in bad_only_imports
                if not any(other != imp and other.startswith(imp + ".") for other in bad_only_imports)
            )
            is_debug = all(imp.split(".")[0] in DEBUG_MODULES for imp in chosen_imps)
            layer = "lint" if is_debug else "architecture"
            src_prefixes: list[str] = []
            if not is_debug:
                forb_pkg_prefixes = tuple(imp.replace(".", "/") + "/" for imp in chosen_imps)
                cand_dirs: set[str] = set()
                for m in b_file_maps:
                    for rel, path in m.items():
                        if path.suffix != ".py" or "/" not in rel:
                            continue
                        parent_prefix = rel.rsplit("/", 1)[0] + "/"
                        if not parent_prefix.startswith(forb_pkg_prefixes):
                            cand_dirs.add(parent_prefix)
                if cand_dirs:
                    src_prefixes = sorted(cand_dirs)
            return ClassificationPlan(
                kind=layer,
                decidable="yes",
                layer=layer,
                tool="script",
                check_input="tree",
                layer_reason=(
                    f"Debug import `{chosen_imps[0]}` is detectable via single-file Python AST."
                    if is_debug
                    else f"Disallowed module dependency `{chosen_imps[0]}` is enforceable at the import boundary via Python AST."
                ),
                strategy="py_import_boundary",
                details={"forbidden_imports": chosen_imps, "source_prefixes": src_prefixes},
            )

        if all(f.has_type_ignore for f in b_feats) and not any(f.has_type_ignore for f in g_feats):
            return ClassificationPlan(
                kind="type",
                decidable="yes",
                layer="type",
                tool="script",
                check_input="tree",
                layer_reason="`# type: ignore` suppression is detectable via Python tokenization at the type layer.",
                strategy="py_type_ignore",
            )

        bad_only_kw = set.intersection(*(f.call_kwargs for f in b_feats)) - set.union(
            *(f.call_kwargs for f in g_feats)
        )
        if bad_only_kw:
            ranked_kw = sorted(
                bad_only_kw,
                key=lambda item: (
                    0 if (item[1], item[2]) in HAZARD_KWARGS_PRIORITY else 1,
                    item[0],
                    item[1],
                ),
            )
            cname, kw_name, kw_repr = ranked_kw[0]
            return ClassificationPlan(
                kind="lint",
                decidable="yes",
                layer="lint",
                tool="script",
                check_input="tree",
                layer_reason=f"Call `{cname}(..., {kw_name}={kw_repr})` is detectable in a single file via Python AST.",
                strategy="py_ast_call_kw",
                details={"call": cname, "kwarg": kw_name, "value_repr": kw_repr},
            )

        local_defs_union = set.union(*(f.local_defs for f in b_feats + g_feats))
        bad_only_calls = (
            set.intersection(*(f.calls for f in b_feats))
            - set.union(*(f.calls for f in g_feats))
            - local_defs_union
        )
        if bad_only_calls:
            ranked_calls = sorted(
                bad_only_calls,
                key=lambda c: (
                    HAZARD_CALLS_PRIORITY.index(c) if c in HAZARD_CALLS_PRIORITY else 999,
                    c,
                ),
            )
            chosen_call = ranked_calls[0]
            return ClassificationPlan(
                kind="lint",
                decidable="yes",
                layer="lint",
                tool="script",
                check_input="tree",
                layer_reason=f"Call to `{chosen_call}(...)` is detectable in a single file via Python AST.",
                strategy="py_ast_call",
                details={"call": chosen_call},
            )

        if all(f.has_bare_except for f in b_feats) and not any(f.has_bare_except for f in g_feats):
            return ClassificationPlan(
                kind="lint",
                decidable="yes",
                layer="lint",
                tool="script",
                check_input="tree",
                layer_reason="Bare `except:` handler is detectable in a single file via Python AST.",
                strategy="py_bare_except",
            )

        if all(f.has_mutable_default for f in b_feats) and not any(f.has_mutable_default for f in g_feats):
            return ClassificationPlan(
                kind="lint",
                decidable="yes",
                layer="lint",
                tool="script",
                check_input="tree",
                layer_reason="Mutable default argument literal is detectable in a single file via Python AST.",
                strategy="py_mutable_default",
            )

    # 3.3 TypeScript / JS type-check escapes (`@ts-ignore`, `@ts-expect-error`, `as any`, `: any`)
    ts_plan = _detect_ts_type_escape(b_file_maps, g_file_maps)
    if ts_plan is not None:
        return ts_plan

    # 3.4 Structured JSON / YAML scalar setting discriminator
    b_cfg = [_extract_config_features(d) for d in bad_dirs]
    g_cfg = [_extract_config_features(d) for d in good_dirs]
    if all(files for files, _ in b_cfg) and all(files for files, _ in g_cfg):
        shared_cfg_files = set.intersection(*(files for files, _ in b_cfg + g_cfg))
        if shared_cfg_files:
            bad_only_kv = set.intersection(*(kv for _, kv in b_cfg)) - set.union(*(kv for _, kv in g_cfg))
            if bad_only_kv:
                key_path, val_json = sorted(bad_only_kv)[0]
                return ClassificationPlan(
                    kind="lint",
                    decidable="yes",
                    layer="lint",
                    tool="script",
                    check_input="tree",
                    layer_reason=f"Structured config setting `{key_path} = {val_json}` is inspectable directly in the tree.",
                    strategy="config_key_value",
                    details={
                        "key_path": key_path,
                        "value_json": val_json,
                        "target_paths": sorted(shared_cfg_files),
                    },
                )

    # 3.5 Non-Python code pattern (with ast-grep / semgrep selection when configured in the repo)
    code_plan = _detect_non_py_code_pattern(b_file_maps, g_file_maps, toolchain=toolchain, explicit_tool=explicit_tool)
    if code_plan is not None:
        return code_plan

    # 3.6 Multi-variant behavioral / property invariant (`layer: verify`)
    summary = _summarize_tree_diff(bad_dirs, good_dirs)
    if multi:
        target_mod, target_funcs = _find_changed_py_targets(bad_dirs, good_dirs)
        return ClassificationPlan(
            kind="data",
            decidable="yes",
            layer="verify",
            tool="property",
            check_input="tree",
            layer_reason="Multiple bad/good variants differ in runtime behavior rather than static syntax; use a seeded property check.",
            strategy="verify_property",
            details={"summary": summary, "target_module": target_mod, "target_funcs": target_funcs},
        )

    # 3.7 Test / suite files -> `layer: test`
    if common_bad_rels and all(
        r.startswith("tests/") or Path(r).name.startswith(("test_", "run_suite")) for r in common_bad_rels
    ):
        return ClassificationPlan(
            kind="test",
            decidable="yes",
            layer="test",
            tool="test",
            check_input="tree",
            layer_reason="Failure lives in test suite / runner behavior.",
            strategy="manual_template",
            details={"summary": summary},
        )

    return ClassificationPlan(
        kind="lint",
        decidable="yes",
        layer="lint",
        tool="script",
        check_input="tree",
        layer_reason="Single-tree snapshot failure; inspect files under --root.",
        strategy="manual_template",
        details={"summary": summary},
    )


def _detect_ts_type_escape(
    b_maps: list[dict[str, Path]], g_maps: list[dict[str, Path]]
) -> ClassificationPlan | None:
    patterns = (
        ("@ts-ignore", r"@ts-ignore\b"),
        ("@ts-expect-error", r"@ts-expect-error\b"),
        ("as any", r"\bas\s+any\b"),
        (": any", r":\s*any\b"),
    )
    ts_exts = {".ts", ".tsx"}
    for label, pat in patterns:
        rx = re.compile(pat)
        in_all_bad = all(
            any(rx.search(p.read_text(encoding="utf-8", errors="replace")) for p in m.values() if p.suffix in ts_exts)
            for m in b_maps
        )
        in_any_good = any(
            any(rx.search(p.read_text(encoding="utf-8", errors="replace")) for p in m.values() if p.suffix in ts_exts)
            for m in g_maps
        )
        if in_all_bad and not in_any_good:
            return ClassificationPlan(
                kind="type",
                decidable="yes",
                layer="type",
                tool="script",
                check_input="tree",
                layer_reason=f"TypeScript type escape `{label}` is detectable directly in `.ts`/`.tsx` files.",
                strategy="ts_type_escape",
                details={"label": label, "regex": pat},
            )
    return None


def _detect_non_py_code_pattern(
    b_maps: list[dict[str, Path]],
    g_maps: list[dict[str, Path]],
    *,
    toolchain: RepoToolchain,
    explicit_tool: str | None,
) -> ClassificationPlan | None:
    if not b_maps or not g_maps:
        return None
    non_py_exts = sorted({p.suffix.lower() for m in b_maps for p in m.values() if p.suffix.lower() in CODE_SUFFIXES})
    if not non_py_exts:
        return None

    good_lines_all: set[str] = set()
    for m in g_maps:
        for p in m.values():
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = ln.strip()
                if s:
                    good_lines_all.add(s)

    bad_line_sets: list[set[str]] = []
    for m in b_maps:
        s_set: set[str] = set()
        for p in m.values():
            if p.suffix.lower() not in CODE_SUFFIXES:
                continue
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = ln.strip()
                if len(s) >= 5 and not s.startswith(("//", "#", "/*", "*")) and s not in {"{", "}", "};", ");"}:
                    s_set.add(s)
        bad_line_sets.append(s_set)

    candidates = sorted(set.intersection(*bad_line_sets) - good_lines_all) if bad_line_sets else []
    if not candidates:
        return None

    chosen_line = candidates[0]
    ext = non_py_exts[0]
    if explicit_tool == "ast-grep" or (explicit_tool is None and toolchain.ast_grep_bin and toolchain.has_ast_grep_config):
        return ClassificationPlan(
            kind="lint",
            decidable="yes",
            layer="lint",
            tool="ast-grep",
            check_input="tree",
            layer_reason=f"AST pattern `{chosen_line}` in `{ext}` matches repository ast-grep toolchain.",
            strategy="ast_grep_bundle",
            details={"pattern": chosen_line, "ext": ext},
        )
    if explicit_tool == "semgrep" or (explicit_tool is None and toolchain.semgrep_bin and toolchain.has_semgrep_config):
        return ClassificationPlan(
            kind="lint",
            decidable="yes",
            layer="lint",
            tool="semgrep",
            check_input="tree",
            layer_reason=f"Code pattern `{chosen_line}` in `{ext}` matches repository semgrep toolchain.",
            strategy="semgrep_bundle",
            details={"pattern": chosen_line, "ext": ext},
        )
    return ClassificationPlan(
        kind="lint",
        decidable="yes",
        layer="lint",
        tool="script",
        check_input="tree",
        layer_reason=f"Disallowed statement `{chosen_line}` in `{ext}` files is inspectable directly in the tree.",
        strategy="text_added_pattern",
        details={"pattern": chosen_line, "suffixes": non_py_exts},
    )


def _find_changed_py_targets(bad_dirs: list[Path], good_dirs: list[Path]) -> tuple[str, list[str]]:
    if not bad_dirs or not good_dirs:
        return "", []
    bf = _tree_files(bad_dirs[0])
    gf = _tree_files(good_dirs[0])
    for rel in sorted(set(bf) & set(gf)):
        if not rel.endswith(".py") or bf[rel].read_bytes() == gf[rel].read_bytes():
            continue
        feats = _extract_py_features(good_dirs[0])
        funcs = (feats.func_names_by_file.get(rel) if feats else None) or []
        return rel, funcs
    return "", []


# ---------------------------------------------------------------------------
# Provisional Check Rendering (provisional draft)
# ---------------------------------------------------------------------------


def _meta_header(
    *,
    check_id: str,
    incident_id: str,
    check_input: str,
    layer: str,
    tool: str,
    docstring: str,
) -> str:
    safe_doc = " ".join(docstring.splitlines()).replace('"""', "'''").strip() or f"Check for {incident_id}."
    return (
        "#!/usr/bin/env python3\n"
        "# ---\n"
        f"# id: {check_id}\n"
        f"# incident: {incident_id}\n"
        f"# input: {check_input}\n"
        f"# layer: {layer}\n"
        f"# tool: {tool}\n"
        "# ---\n"
        f'"""{safe_doc}"""\n\n'
        "from __future__ import annotations\n\n"
    )


def render_draft_bundle(
    *,
    plan: ClassificationPlan,
    check_id: str,
    incident_id: str,
    message: str,
    base_rel_stem: str,
    allow_provisional: bool = True,
) -> DraftBundle:
    """Render either a single-file `.py` check or a `<dir>/{check.py, rule.yml}` bundle."""
    if plan.tool in ("semgrep", "ast-grep"):
        return _render_external_linter_bundle(
            plan=plan,
            check_id=check_id,
            incident_id=incident_id,
            message=message,
            bundle_rel_dir=base_rel_stem,
            allow_provisional=allow_provisional,
        )

    rel_py = base_rel_stem if base_rel_stem.endswith(".py") else f"{base_rel_stem}.py"
    if not allow_provisional or plan.strategy in ("manual_template", "verify_property"):
        code = render_manual_scaffold(
            plan=plan,
            check_id=check_id,
            incident_id=incident_id,
            message=message,
            rel_path=rel_py,
        )
        return DraftBundle(
            stored_rel=rel_py,
            program_rel=rel_py,
            files={rel_py: code},
            is_provisional_rule=False,
            strategy=plan.strategy,
        )

    hdr = _meta_header(
        check_id=check_id,
        incident_id=incident_id,
        check_input=plan.check_input,
        layer=plan.layer,
        tool=plan.tool,
        docstring=message,
    )
    body = _render_provisional_python_body(plan, message=message, rel_path=rel_py)
    if body is None:
        code = render_manual_scaffold(
            plan=plan,
            check_id=check_id,
            incident_id=incident_id,
            message=message,
            rel_path=rel_py,
        )
        return DraftBundle(
            stored_rel=rel_py,
            program_rel=rel_py,
            files={rel_py: code},
            is_provisional_rule=False,
            strategy="manual_template",
        )
    full = hdr + body + '\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    return DraftBundle(
        stored_rel=rel_py,
        program_rel=rel_py,
        files={rel_py: full},
        is_provisional_rule=True,
        strategy=plan.strategy,
    )


_WALK_HELPER = (
    'SKIP_DIRS = {".git", ".make-it-lint", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache"}\n\n\n'
    "def _iter_files(root: Path, suffixes: set[str] | None = None):\n"
    "    for dirpath, dirnames, filenames in os.walk(root):\n"
    "        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)\n"
    "        base = Path(dirpath)\n"
    "        for fname in sorted(filenames):\n"
    "            p = base / fname\n"
    "            if suffixes is None or p.suffix.lower() in suffixes:\n"
    "                yield p\n\n\n"
)


def _render_provisional_python_body(plan: ClassificationPlan, *, message: str, rel_path: str) -> str | None:
    _ = rel_path
    s = plan.strategy
    d = plan.details
    msg_repr = repr(message)

    if s == "tree_forbidden_path":
        paths_lit = repr(list(d["forbidden_paths"]))
        return (
            "import argparse\nimport json\nimport os\nfrom pathlib import Path\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            f'    msg = str(cfg.get("message") or {msg_repr})\n'
            f'    forbidden = cfg.get("forbidden_paths") or {paths_lit}\n'
            "    hits: list[str] = []\n"
            "    for rel in forbidden:\n"
            "        if (root / rel).exists():\n"
            '            hits.append(f"{rel}: {msg}")\n'
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "py_import_boundary":
        imps_lit = repr(list(d["forbidden_imports"]))
        src_pref_lit = repr(list(d.get("source_prefixes") or []))
        return (
            "import argparse\nimport ast\nimport json\nimport os\nfrom pathlib import Path\n\n"
            + _WALK_HELPER
            + "def _matches_forbidden(name: str, forbidden: list[str]) -> bool:\n"
            "    return any(name == item or name.startswith(item + '.') for item in forbidden)\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            f'    msg = str(cfg.get("message") or {msg_repr})\n'
            f'    forbidden = list(cfg.get("forbidden_imports") or {imps_lit})\n'
            f'    source_prefixes = tuple(cfg.get("source_prefixes") if "source_prefixes" in cfg else {src_pref_lit})\n'
            "    hits: list[str] = []\n"
            '    for path in _iter_files(root, {".py"}):\n'
            "        rel = path.relative_to(root).as_posix()\n"
            "        if source_prefixes and not rel.startswith(source_prefixes):\n"
            "            continue\n"
            "        try:\n"
            '            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))\n'
            "        except SyntaxError:\n"
            "            continue\n"
            "        for node in ast.walk(tree):\n"
            "            if isinstance(node, ast.Import):\n"
            "                for alias in node.names:\n"
            "                    if _matches_forbidden(alias.name, forbidden):\n"
            '                        hits.append(f"{rel}:{node.lineno}: disallow importing {alias.name} ({msg})")\n'
            "            elif isinstance(node, ast.ImportFrom) and node.module:\n"
            "                if _matches_forbidden(node.module, forbidden):\n"
            '                    hits.append(f"{rel}:{node.lineno}: disallow importing {node.module} ({msg})")\n'
            "    for hit in sorted(set(hits)):\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "py_type_ignore":
        return (
            "import argparse\nimport io\nimport os\nimport re\nimport tokenize\nfrom pathlib import Path\n\n"
            + _WALK_HELPER
            + 'TYPE_IGNORE_RE = re.compile(r"#\\s*type:\\s*ignore\\b")\n\n\n'
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            f"    msg = {msg_repr}\n"
            "    hits: list[str] = []\n"
            '    for path in _iter_files(root, {".py"}):\n'
            '        src = path.read_text(encoding="utf-8", errors="replace")\n'
            "        rel = path.relative_to(root).as_posix()\n"
            "        try:\n"
            "            for tok in tokenize.generate_tokens(io.StringIO(src).readline):\n"
            "                if tok.type == tokenize.COMMENT and TYPE_IGNORE_RE.search(tok.string):\n"
            '                    hits.append(f"{rel}:{tok.start[0]}: {msg}")\n'
            "        except tokenize.TokenError:\n"
            "            continue\n"
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "py_ast_call_kw":
        call_lit = repr(d["call"])
        kw_lit = repr(d["kwarg"])
        val_repr = d["value_repr"]
        return (
            "import argparse\nimport ast\nimport os\nfrom pathlib import Path\n\n"
            + _WALK_HELPER
            + "def _dotted(node: ast.AST) -> str | None:\n"
            "    if isinstance(node, ast.Name):\n"
            "        return node.id\n"
            "    if isinstance(node, ast.Attribute):\n"
            "        head = _dotted(node.value)\n"
            '        return f"{head}.{node.attr}" if head else None\n'
            "    return None\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            f"    msg = {msg_repr}\n"
            "    hits: list[str] = []\n"
            '    for path in _iter_files(root, {".py"}):\n'
            "        try:\n"
            '            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))\n'
            "        except SyntaxError:\n"
            "            continue\n"
            "        rel = path.relative_to(root).as_posix()\n"
            "        for node in ast.walk(tree):\n"
            f"            if isinstance(node, ast.Call) and _dotted(node.func) == {call_lit}:\n"
            "                for kw in node.keywords:\n"
            f"                    if kw.arg == {kw_lit} and isinstance(kw.value, ast.Constant) and kw.value.value == {val_repr}:\n"
            '                        hits.append(f"{rel}:{node.lineno}: {msg}")\n'
            "    for hit in sorted(set(hits)):\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "py_ast_call":
        call_lit = repr(d["call"])
        return (
            "import argparse\nimport ast\nimport json\nimport os\nfrom pathlib import Path\n\n"
            + _WALK_HELPER
            + "def _dotted(node: ast.AST) -> str | None:\n"
            "    if isinstance(node, ast.Name):\n"
            "        return node.id\n"
            "    if isinstance(node, ast.Attribute):\n"
            "        head = _dotted(node.value)\n"
            '        return f"{head}.{node.attr}" if head else None\n'
            "    return None\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            f'    msg = str(cfg.get("message") or {msg_repr})\n'
            f'    forbidden_calls = set(cfg.get("forbidden_calls") or [{call_lit}])\n'
            "    hits: list[str] = []\n"
            '    for path in _iter_files(root, {".py"}):\n'
            "        try:\n"
            '            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))\n'
            "        except SyntaxError:\n"
            "            continue\n"
            "        rel = path.relative_to(root).as_posix()\n"
            "        for node in ast.walk(tree):\n"
            "            if isinstance(node, ast.Call):\n"
            "                cname = _dotted(node.func)\n"
            "                if cname in forbidden_calls:\n"
            '                    hits.append(f"{rel}:{node.lineno}: disallow {cname}() ({msg})")\n'
            "    for hit in sorted(set(hits)):\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "py_bare_except":
        return (
            "import argparse\nimport ast\nimport os\nfrom pathlib import Path\n\n"
            + _WALK_HELPER
            + "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            f"    msg = {msg_repr}\n"
            "    hits: list[str] = []\n"
            '    for path in _iter_files(root, {".py"}):\n'
            "        try:\n"
            '            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))\n'
            "        except SyntaxError:\n"
            "            continue\n"
            "        rel = path.relative_to(root).as_posix()\n"
            "        for node in ast.walk(tree):\n"
            "            if isinstance(node, ast.ExceptHandler) and node.type is None:\n"
            '                hits.append(f"{rel}:{node.lineno}: catch a specific exception instead of bare except ({msg})")\n'
            "    for hit in sorted(set(hits)):\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "py_mutable_default":
        return (
            "import argparse\nimport ast\nimport os\nfrom pathlib import Path\n\n"
            + _WALK_HELPER
            + "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            f"    msg = {msg_repr}\n"
            "    hits: list[str] = []\n"
            '    for path in _iter_files(root, {".py"}):\n'
            "        try:\n"
            '            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))\n'
            "        except SyntaxError:\n"
            "            continue\n"
            "        rel = path.relative_to(root).as_posix()\n"
            "        for node in ast.walk(tree):\n"
            "            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):\n"
            "                defaults = list(node.args.defaults) + [x for x in node.args.kw_defaults if x is not None]\n"
            "                if any(isinstance(x, (ast.List, ast.Dict, ast.Set)) for x in defaults):\n"
            '                    hits.append(f"{rel}:{node.lineno}: use None instead of a mutable default in {node.name}() ({msg})")\n'
            "    for hit in sorted(set(hits)):\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "ts_type_escape":
        pat_lit = repr(d["regex"])
        return (
            "import argparse\nimport os\nimport re\nfrom pathlib import Path\n\n"
            + _WALK_HELPER
            + f"ESCAPE_RE = re.compile({pat_lit})\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            f"    msg = {msg_repr}\n"
            "    hits: list[str] = []\n"
            '    for path in _iter_files(root, {".ts", ".tsx"}):\n'
            "        rel = path.relative_to(root).as_posix()\n"
            '        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):\n'
            "            if ESCAPE_RE.search(line):\n"
            '                hits.append(f"{rel}:{lineno}: {msg}")\n'
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "config_key_value":
        key_parts_lit = repr(d["key_path"].split("."))
        expected_val = repr(json.loads(d["value_json"]))
        tpaths_lit = repr(list(d.get("target_paths") or []))
        return (
            "import argparse\nimport json\nimport os\nfrom pathlib import Path\nfrom mil import yamlio\n\n"
            + _WALK_HELPER
            + "def _lookup(mapping: object, parts: list[str]) -> tuple[bool, object]:\n"
            "    cur = mapping\n"
            "    for part in parts:\n"
            "        if not isinstance(cur, dict) or part not in cur:\n"
            "            return False, None\n"
            "        cur = cur[part]\n"
            "    return True, cur\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            f'    msg = str(cfg.get("message") or {msg_repr})\n'
            f'    target_paths = set(cfg.get("target_paths") if "target_paths" in cfg else {tpaths_lit})\n'
            "    hits: list[str] = []\n"
            '    for path in _iter_files(root, {".json", ".yaml", ".yml"}):\n'
            "        rel = path.relative_to(root).as_posix()\n"
            '        if target_paths and "*" not in target_paths and rel not in target_paths:\n'
            "            continue\n"
            '        text = path.read_text(encoding="utf-8", errors="replace")\n'
            "        try:\n"
            '            data = json.loads(text) if path.suffix.lower() == ".json" else yamlio.load(text)\n'
            "        except (ValueError, yamlio.YAMLError):\n"
            "            continue\n"
            f"        found, val = _lookup(data, {key_parts_lit})\n"
            f"        if found and val == {expected_val}:\n"
            '            hits.append(f"{rel}: {msg}")\n'
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s == "text_added_pattern":
        pat_lit = repr(d["pattern"])
        sfx_lit = repr(set(d["suffixes"]))
        return (
            "import argparse\nimport os\nfrom pathlib import Path\n\n"
            + _WALK_HELPER
            + "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    root = Path(args.root)\n"
            f"    msg = {msg_repr}\n"
            f"    suffixes = {sfx_lit}\n"
            f"    needle = {pat_lit}\n"
            "    hits: list[str] = []\n"
            "    for path in _iter_files(root, suffixes):\n"
            "        rel = path.relative_to(root).as_posix()\n"
            '        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):\n'
            "            if needle in line:\n"
            '                hits.append(f"{rel}:{lineno}: {msg}")\n'
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    if s in ("diff_allowed_prefixes", "diff_forbidden_paths", "diff_cochange"):
        return _render_diff_provisional_body(plan, msg_repr=msg_repr)

    if s in ("action_command", "action_path"):
        return _render_action_provisional_body(plan, msg_repr=msg_repr)

    return None


def _render_diff_provisional_body(plan: ClassificationPlan, *, msg_repr: str) -> str:
    d = plan.details
    common = (
        "import argparse\nimport json\nimport os\nfrom pathlib import Path\n\n\n"
        "def _files(tree: Path) -> dict[str, Path]:\n"
        "    out: dict[str, Path] = {}\n"
        '    for p in sorted(tree.rglob("*")):\n'
        "        if not p.is_file():\n"
        "            continue\n"
        "        parts = p.relative_to(tree).parts\n"
        '        if any(part in {".git", ".make-it-lint", "__pycache__", ".gitkeep"} for part in parts):\n'
        "            continue\n"
        "        rel = p.relative_to(tree).as_posix()\n"
        '        if rel != "pr.yaml":\n'
        "            out[rel] = p\n"
        "    return out\n\n\n"
        "def _changed(base: Path, head: Path) -> list[str]:\n"
        "    bf, hf = _files(base), _files(head)\n"
        "    return sorted(\n"
        "        rel for rel in (set(bf) | set(hf))\n"
        "        if rel not in bf or rel not in hf or bf[rel].read_bytes() != hf[rel].read_bytes()\n"
        "    )\n\n\n"
    )
    if plan.strategy == "diff_cochange":
        trig_lit = repr(list(d["triggers"]))
        comp_lit = repr(list(d["companions"]))
        return common + (
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--base", required=True)\n'
            '    parser.add_argument("--head", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    changed = set(_changed(Path(args.base), Path(args.head)))\n"
            '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            f'    msg = str(cfg.get("message") or {msg_repr})\n'
            f'    triggers = list(cfg.get("triggers") or {trig_lit})\n'
            f'    companions = list(cfg.get("companions") or {comp_lit})\n'
            "    touched_triggers = [p for p in triggers if p in changed]\n"
            "    missing = [p for p in companions if p not in changed]\n"
            "    if touched_triggers and missing:\n"
            '        missing_list = ", ".join(missing)\n'
            '        print(f"{touched_triggers[0]}: also update {missing_list} ({msg})")\n'
            "        return 1\n"
            "    return 0\n"
        )

    pr_kind_lit = repr(d.get("pr_kind") or "")
    br_prefix_lit = repr(d.get("branch_prefix") or "")
    scope_guard = (
        f'    req_kind = cfg.get("pr_kind", {pr_kind_lit})\n'
        f'    req_branch_prefix = cfg.get("branch_prefix", {br_prefix_lit})\n'
        '    cur_kind = os.environ.get("MIL_PR_KIND", "")\n'
        '    cur_branch = os.environ.get("MIL_PR_BRANCH", "")\n'
        '    cur_labels = {x.strip() for x in os.environ.get("MIL_PR_LABELS", "").split(",") if x.strip()}\n'
        "    if req_kind or req_branch_prefix:\n"
        "        matches_scope = (\n"
        "            (bool(req_kind) and (cur_kind == req_kind or req_kind in cur_labels))\n"
        "            or (bool(req_branch_prefix) and cur_branch.startswith(req_branch_prefix))\n"
        "        )\n"
        "        if not matches_scope:\n"
        "            return 0\n"
    )
    if plan.strategy == "diff_allowed_prefixes":
        allowed_lit = repr(list(d["allowed_prefixes"]))
        return common + (
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--base", required=True)\n'
            '    parser.add_argument("--head", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            f'    msg = str(cfg.get("message") or {msg_repr})\n'
            + scope_guard
            + f'    allowed = tuple(cfg.get("allowed_prefixes") or {allowed_lit})\n'
            "    hits = [\n"
            '        f"{rel}: path is outside allowed scope {list(allowed)} ({msg})"\n'
            "        for rel in _changed(Path(args.base), Path(args.head))\n"
            '        if not rel.startswith(".make-it-lint/") and not rel.startswith(allowed)\n'
            "    ]\n"
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    forb_lit = repr(list(d["forbidden_paths"]))
    return common + (
        "def main() -> int:\n"
        "    parser = argparse.ArgumentParser()\n"
        '    parser.add_argument("--base", required=True)\n'
        '    parser.add_argument("--head", required=True)\n'
        '    parser.add_argument("--config", required=True)\n'
        "    args = parser.parse_args()\n"
        '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
        f'    msg = str(cfg.get("message") or {msg_repr})\n'
        + scope_guard
        + f'    forbidden = set(cfg.get("forbidden_paths") or {forb_lit})\n'
        "    hits = [\n"
        '        f"{rel}: {msg}"\n'
        "        for rel in _changed(Path(args.base), Path(args.head))\n"
        "        if rel in forbidden\n"
        "    ]\n"
        "    for hit in hits:\n"
        "        print(hit)\n"
        "    return 1 if hits else 0\n"
    )


def _render_action_provisional_body(plan: ClassificationPlan, *, msg_repr: str) -> str:
    d = plan.details
    if plan.strategy == "action_command":
        prefix_lit = repr(list(d["prefix"]))
        req_lit = repr(list(d["required_tokens"]))
        return (
            "import argparse\nimport json\nimport os\nimport shlex\nfrom pathlib import Path\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--action", required=True)\n'
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            '    action = json.loads(Path(args.action).read_text(encoding="utf-8"))\n'
            '    if action.get("kind") != "command":\n'
            "        return 0\n"
            '    cmd = str(action.get("command") or "")\n'
            "    try:\n"
            "        tokens = shlex.split(cmd, posix=True)\n"
            "    except ValueError:\n"
            "        tokens = cmd.split()\n"
            '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            f'    msg = str(cfg.get("message") or {msg_repr})\n'
            f'    prefix = list(cfg.get("command_prefix") or {prefix_lit})\n'
            f'    required = list(cfg.get("required_tokens") or {req_lit})\n'
            "    if len(tokens) >= len(prefix) and tokens[: len(prefix)] == prefix:\n"
            "        rest = set(tokens[len(prefix) :])\n"
            "        if all(tok in rest for tok in required):\n"
            '            print(f"{prefix[0]}: {msg}")\n'
            "            return 1\n"
            "    return 0\n"
        )

    kinds_lit = repr(set(d["kinds"]))
    paths_lit = repr(set(d["forbidden_paths"]))
    return (
        "import argparse\nimport json\nimport os\nfrom pathlib import Path\n\n\n"
        "def main() -> int:\n"
        "    parser = argparse.ArgumentParser()\n"
        '    parser.add_argument("--action", required=True)\n'
        '    parser.add_argument("--root", required=True)\n'
        '    parser.add_argument("--config", required=True)\n'
        "    args = parser.parse_args()\n"
        '    action = json.loads(Path(args.action).read_text(encoding="utf-8"))\n'
        f'    if action.get("kind") not in {kinds_lit}:\n'
        "        return 0\n"
        '    cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
        f'    msg = str(cfg.get("message") or {msg_repr})\n'
        f'    forbidden = set(cfg.get("forbidden_paths") or {paths_lit})\n'
        '    paths = list(action.get("paths") or ([action["path"]] if action.get("path") else []))\n'
        '    hits = [f"{p}: {msg}" for p in paths if p in forbidden]\n'
        "    for hit in hits:\n"
        "        print(hit)\n"
        "    return 1 if hits else 0\n"
    )


def _render_external_linter_bundle(
    *,
    plan: ClassificationPlan,
    check_id: str,
    incident_id: str,
    message: str,
    bundle_rel_dir: str,
    allow_provisional: bool,
) -> DraftBundle:
    bundle_dir = bundle_rel_dir[:-3] if bundle_rel_dir.endswith(".py") else bundle_rel_dir
    prog_rel = f"{bundle_dir}/check.py"
    rule_rel = f"{bundle_dir}/rule.yml"
    pattern = str(plan.details.get("pattern") or "")
    ext = str(plan.details.get("ext") or ".py")
    has_pat = bool(allow_provisional and pattern)

    hdr = _meta_header(
        check_id=check_id,
        incident_id=incident_id,
        check_input="tree",
        layer=plan.layer,
        tool=plan.tool,
        docstring=message,
    )

    if plan.tool == "semgrep":
        lang = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".rb": "ruby",
            ".sh": "bash",
        }.get(ext, "generic")
        pat_val = json.dumps(pattern if has_pat else "$UNIMPLEMENTED_SEMGREP_PATTERN")
        msg_val = json.dumps(message, ensure_ascii=False)
        rule_yaml = (
            "rules:\n"
            f"  - id: {check_id}\n"
            f"    message: {msg_val}\n"
            "    severity: ERROR\n"
            f"    languages: [{lang}]\n"
            f"    pattern: {pat_val}\n"
        )
        runner_body = (
            "import argparse\nimport json\nimport shutil\nimport subprocess\nimport sys\nfrom pathlib import Path\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    rule = Path(__file__).resolve().parent / \"rule.yml\"\n"
            '    if "$UNIMPLEMENTED_SEMGREP_PATTERN" in rule.read_text(encoding="utf-8"):\n'
            f'        sys.stderr.write("{rule_rel}: edit pattern in rule.yml before running mil prove\\n")\n'
            "        return 2\n"
            '    semgrep = shutil.which("semgrep")\n'
            "    if not semgrep:\n"
            '        sys.stderr.write("semgrep executable not found on PATH\\n")\n'
            "        return 2\n"
            "    root = Path(args.root).resolve()\n"
            "    proc = subprocess.run(\n"
            '        [semgrep, "scan", "--config", str(rule), "--json", "--quiet", str(root)],\n'
            "        capture_output=True, text=True, encoding=\"utf-8\", errors=\"replace\"\n"
            "    )\n"
            "    if proc.returncode not in (0, 1):\n"
            "        sys.stderr.write(proc.stderr or proc.stdout or f\"semgrep exited {proc.returncode}\\n\")\n"
            "        return 2\n"
            "    payload = json.loads(proc.stdout or \"{}\")\n"
            "    hits: list[str] = []\n"
            '    for item in payload.get("results") or []:\n'
            '        p = Path(item.get("path", ""))\n'
            "        rel = p.resolve().relative_to(root).as_posix() if p.is_absolute() else p.as_posix()\n"
            '        if any(seg in {".git", ".make-it-lint", ".venv", "venv", "node_modules", "__pycache__"} for seg in rel.split("/")):\n'
            "            continue\n"
            '        line = (item.get("start") or {}).get("line") or 1\n'
            f'        msg = ((item.get("extra") or {{}}).get("message")) or {repr(message)}\n'
            '        hits.append(f"{rel}:{line}: {msg}")\n'
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )
    else:
        lang = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "jsx",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
        }.get(ext, "python")
        pat_val = json.dumps(pattern if has_pat else "$UNIMPLEMENTED_AST_GREP_PATTERN")
        msg_val = json.dumps(message, ensure_ascii=False)
        rule_yaml = (
            f"id: {check_id}\n"
            f"language: {lang}\n"
            f"message: {msg_val}\n"
            "rule:\n"
            f"  pattern: {pat_val}\n"
        )
        runner_body = (
            "import argparse\nimport json\nimport shutil\nimport subprocess\nimport sys\nfrom pathlib import Path\n\n\n"
            "def _find_ast_grep() -> str | None:\n"
            '    ag = shutil.which("ast-grep")\n'
            "    if ag:\n"
            "        return ag\n"
            '    sg = shutil.which("sg")\n'
            "    if sg:\n"
            '        p = subprocess.run([sg, "--version"], capture_output=True, text=True, errors="replace")\n'
            '        if p.returncode == 0 and "ast-grep" in (p.stdout + p.stderr).lower():\n'
            "            return sg\n"
            "    return None\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n"
            "    rule = Path(__file__).resolve().parent / \"rule.yml\"\n"
            '    if "$UNIMPLEMENTED_AST_GREP_PATTERN" in rule.read_text(encoding="utf-8"):\n'
            f'        sys.stderr.write("{rule_rel}: edit pattern in rule.yml before running mil prove\\n")\n'
            "        return 2\n"
            "    ag = _find_ast_grep()\n"
            "    if not ag:\n"
            '        sys.stderr.write("ast-grep executable not found on PATH\\n")\n'
            "        return 2\n"
            "    root = Path(args.root).resolve()\n"
            "    proc = subprocess.run(\n"
            '        [ag, "scan", "--rule", str(rule), "--json=compact", str(root)],\n'
            "        capture_output=True, text=True, encoding=\"utf-8\", errors=\"replace\"\n"
            "    )\n"
            "    if proc.returncode not in (0, 1):\n"
            "        sys.stderr.write(proc.stderr or proc.stdout or f\"ast-grep exited {proc.returncode}\\n\")\n"
            "        return 2\n"
            "    items = json.loads(proc.stdout or \"[]\")\n"
            "    hits: list[str] = []\n"
            "    for item in items:\n"
            '        p = Path(item.get("file", ""))\n'
            "        rel = p.resolve().relative_to(root).as_posix() if p.is_absolute() else p.as_posix()\n"
            '        if any(seg in {".git", ".make-it-lint", ".venv", "venv", "node_modules", "__pycache__"} for seg in rel.split("/")):\n'
            "            continue\n"
            '        line = ((item.get("range") or {}).get("start") or {}).get("line", 0) + 1\n'
            f'        msg = item.get("message") or {repr(message)}\n'
            '        hits.append(f"{rel}:{line}: {msg}")\n'
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )

    full_runner = hdr + runner_body + '\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    return DraftBundle(
        stored_rel=bundle_dir,
        program_rel=prog_rel,
        files={prog_rel: full_runner, rule_rel: rule_yaml},
        is_provisional_rule=has_pat,
        strategy=plan.strategy,
    )


def render_manual_scaffold(
    *,
    plan: ClassificationPlan,
    check_id: str,
    incident_id: str,
    message: str,
    rel_path: str,
) -> str:
    hdr = _meta_header(
        check_id=check_id,
        incident_id=incident_id,
        check_input=plan.check_input,
        layer=plan.layer,
        tool=plan.tool,
        docstring=message,
    )
    summary_comment = ""
    if plan.details.get("summary"):
        summary_comment = f"    # Fixture summary: {plan.details['summary']}\n"

    if plan.strategy == "verify_property":
        target_mod = str(plan.details.get("target_module") or "module_under_test.py")
        funcs = plan.details.get("target_funcs") or []
        fn_hint = f" (changed functions: {', '.join(funcs)})" if funcs else ""
        body = (
            "import argparse\nimport importlib.util\nimport os\nimport random\nimport sys\nfrom pathlib import Path\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n\n"
            "    root = Path(args.root)\n"
            '    rng = random.Random(int(os.environ.get("MIL_SEED", "0")))\n'
            f'    target = root / {json.dumps(target_mod)}\n'
            "    _ = (rng, importlib.util, target)\n"
            + summary_comment
            + f"    # Replace the two lines below with your seeded property check on {target_mod}{fn_hint}.\n"
            f'    sys.stderr.write("{rel_path}: unimplemented check scaffold; edit the rule logic before running mil prove\\n")\n'
            "    return 2\n"
        )
        return hdr + body + '\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'

    if plan.check_input == "diff":
        body = (
            "import argparse\nimport json\nimport os\nimport sys\nfrom pathlib import Path\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--base", required=True)\n'
            '    parser.add_argument("--head", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n\n"
            "    base = Path(args.base)\n"
            "    head = Path(args.head)\n"
            '    check_cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            "    _ = (base, head, check_cfg)\n"
            "    hits: list[str] = []\n\n"
            + summary_comment
            + "    # Replace the two lines below with your diff check appending '<rel_path>[:<line>]: <how to fix>' to `hits`.\n"
            f'    sys.stderr.write("{rel_path}: unimplemented check scaffold; edit the rule logic before running mil prove\\n")\n'
            "    return 2\n\n"
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )
    elif plan.check_input == "action":
        body = (
            "import argparse\nimport json\nimport os\nimport sys\nfrom pathlib import Path\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--action", required=True)\n'
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n\n"
            '    action = json.loads(Path(args.action).read_text(encoding="utf-8"))\n'
            '    check_cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            "    _ = (action, check_cfg)\n"
            "    hits: list[str] = []\n\n"
            + summary_comment
            + "    # Replace the two lines below with your action guard appending '<rel_path>: <how to fix>' to `hits`.\n"
            f'    sys.stderr.write("{rel_path}: unimplemented check scaffold; edit the rule logic before running mil prove\\n")\n'
            "    return 2\n\n"
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )
    else:
        body = (
            "import argparse\nimport json\nimport os\nimport sys\nfrom pathlib import Path\n\n\n"
            "def main() -> int:\n"
            "    parser = argparse.ArgumentParser()\n"
            '    parser.add_argument("--root", required=True)\n'
            '    parser.add_argument("--config", required=True)\n'
            "    args = parser.parse_args()\n\n"
            "    root = Path(args.root)\n"
            '    check_cfg = json.loads(os.environ.get("MIL_CHECK_CONFIG_JSON") or "{}")\n'
            "    _ = check_cfg\n"
            "    hits: list[str] = []\n\n"
            "    for path in sorted(root.rglob(\"*\")):\n"
            '        if not path.is_file() or any(part in {".git", ".make-it-lint", ".venv", "venv", "node_modules", "__pycache__"} for part in path.relative_to(root).parts):\n'
            "            continue\n"
            "        # Replace the two lines below with your tree check appending '<rel_path>[:<line>]: <how to fix>' to `hits`.\n"
            "        break\n"
            + summary_comment
            + f'    sys.stderr.write("{rel_path}: unimplemented check scaffold; edit the rule logic before running mil prove\\n")\n'
            "    return 2\n\n"
            "    for hit in hits:\n"
            "        print(hit)\n"
            "    return 1 if hits else 0\n"
        )
    return hdr + body + '\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'


def verify_provisional_check(
    root: Path,
    rec_dir: Path,
    rec: dict[str, Any],
    program_entry: Path,
) -> tuple[bool, str]:
    """Dry-run a provisional check against all bad/ and good/ variants without mutating state."""
    proof = rec.get("proof") if isinstance(rec.get("proof"), dict) else {}
    bad = rec_dir / as_rel(str(proof.get("bad") or "bad/"))
    good = rec_dir / as_rel(str(proof.get("good") or "good/"))
    if not bad.is_dir() or not good.is_dir():
        return False, "missing bad/ or good/ directory"

    multi = is_multi_fixture(bad, good)
    bad_vars = list_bad_variants(bad, multi=multi)
    good_vars = list_good_variants(good, multi=multi)
    if not bad_vars or not good_vars:
        return False, "empty fixture variants"

    try:
        program = resolve_check_program(program_entry)
        meta = extract_metadata(program)
    except (CheckError, yamlio.YAMLError) as exc:
        return False, str(exc)

    cfg = config_path(root)
    layer = str(rec.get("layer") or meta.get("layer") or "lint")
    seed = int(proof.get("seed") or 0) if isinstance(proof, dict) and proof.get("seed") is not None else 0

    red_bad = 0
    for _, vdir in bad_vars:
        if not any(c for c in vdir.iterdir() if not c.name.startswith(".")):
            return False, "empty bad/ fixture"
        try:
            proc = invoke_check_reproducible(
                program, meta, layer=layer, seed=seed, root=root, config=cfg, fixture_dir=vdir
            )
        except (CheckError, NonReproducibleError) as exc:
            return False, str(exc)
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if proc.returncode == 1 and lines and all(HIT_RE.match(ln) for ln in lines):
            red_bad += 1

    green_good = 0
    for _, vdir in good_vars:
        if not any(c for c in vdir.iterdir() if not c.name.startswith(".")):
            return False, "empty good/ fixture"
        try:
            proc = invoke_check_reproducible(
                program, meta, layer=layer, seed=seed, root=root, config=cfg, fixture_dir=vdir
            )
        except (CheckError, NonReproducibleError) as exc:
            return False, str(exc)
        if proc.returncode == 0:
            green_good += 1

    summary = proven_result_text(
        red_bad=red_bad,
        n_bad=len(bad_vars),
        green_good=green_good,
        n_good=len(good_vars),
        multi=multi,
    )
    ok = red_bad == len(bad_vars) > 0 and green_good == len(good_vars) > 0
    return ok, summary
