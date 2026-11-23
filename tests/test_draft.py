"""Structured verification tests for automatic layer/tool selection, git-ref fixtures, and provisional checks (provisional draft)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from mil.checks import extract_metadata
from mil.draft import detect_toolchain
from mil.guard import check_protected_write
from mil.ledger import load_record_file
from tests.support import run_mil, scratch_adopter


def _record_auto_and_prove(
    tc: unittest.TestCase,
    root: Path,
    *,
    fingerprint: str,
    title: str,
    what: str,
    bad_dir: Path,
    good_dir: Path,
) -> tuple[str, dict]:
    rec_proc = run_mil(
        "record",
        "--root",
        str(root),
        "--fingerprint",
        fingerprint,
        "--title",
        title,
        "--what",
        what,
        "--source",
        "PR review",
        "--actor",
        "cloud-agent",
        "--reporter",
        "@reviewer",
        "--bad",
        str(bad_dir),
        "--good",
        str(good_dir),
        "--auto",
    )
    tc.assertEqual(rec_proc.returncode, 0, rec_proc.stdout + rec_proc.stderr)
    rec_paths = sorted((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
    rec_path = rec_paths[-1]
    rec = load_record_file(rec_path)
    incident_id = rec["id"]

    scaf = run_mil("scaffold", incident_id, "--root", str(root))
    tc.assertEqual(scaf.returncode, 0, scaf.stdout + scaf.stderr)
    tc.assertIn("drafted provisional check", scaf.stdout, scaf.stdout + scaf.stderr)
    # Record must remain `open` after provisional draft until explicit `mil prove`
    tc.assertEqual(load_record_file(rec_path)["enforcement"]["state"], "open")

    lint_open = run_mil("lint", "--root", str(root))
    tc.assertEqual(lint_open.returncode, 0, lint_open.stdout + lint_open.stderr)

    prove = run_mil("prove", incident_id, "--check-hunks", "--root", str(root))
    tc.assertEqual(prove.returncode, 0, prove.stdout + prove.stderr)
    final_rec = load_record_file(rec_path)
    tc.assertEqual(final_rec["enforcement"]["state"], "proven")
    return incident_id, final_rec


class AutoSelectAndProvisionalDraftTests(unittest.TestCase):
    def test_architecture_import_boundary_ignores_docstring_mentions(self):
        root = scratch_adopter()
        fx = Path(tempfile.mkdtemp(prefix="mil-fx-arch-"))
        self.addCleanup(shutil.rmtree, fx, True)
        (fx / "bad/billing/domain").mkdir(parents=True)
        (fx / "good/billing/domain").mkdir(parents=True)
        (fx / "bad/billing/domain/invoice_calculator.py").write_text(
            "from decimal import Decimal\n"
            "from billing.infra.stripe_gateway import fetch_live_fx_rate\n\n"
            "def compute_total_jpy(subtotal_usd: Decimal) -> Decimal:\n"
            "    return subtotal_usd * fetch_live_fx_rate('USD', 'JPY')\n",
            encoding="utf-8",
        )
        # Good fixture mentions `billing.infra.stripe_gateway` in a docstring to verify AST accuracy
        (fx / "good/billing/domain/invoice_calculator.py").write_text(
            '"""Pure domain calculator; never import billing.infra.stripe_gateway here."""\n'
            "from decimal import Decimal\n"
            "from billing.domain.ports import FxRatePort\n\n"
            "def compute_total_jpy(subtotal_usd: Decimal, fx: FxRatePort) -> Decimal:\n"
            "    return subtotal_usd * fx.rate('USD', 'JPY')\n",
            encoding="utf-8",
        )

        _, rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="architecture:domain-imports-stripe-gateway",
            title="Domain calculator imported Stripe infrastructure adapter directly",
            what="billing/domain/invoice_calculator.py imported billing.infra.stripe_gateway.",
            bad_dir=fx / "bad",
            good_dir=fx / "good",
        )
        self.assertEqual(rec["layer"], "architecture")
        self.assertEqual(rec["classification"]["kind"], "architecture")

    def test_type_layer_python_type_ignore_and_typescript_escape(self):
        root = scratch_adopter()
        fx = Path(tempfile.mkdtemp(prefix="mil-fx-type-"))
        self.addCleanup(shutil.rmtree, fx, True)

        # 1. Python `# type: ignore` comment (with string-literal edge case in good/)
        (fx / "py_bad/pricing").mkdir(parents=True)
        (fx / "py_good/pricing").mkdir(parents=True)
        (fx / "py_bad/pricing/margin_solver.py").write_text(
            "from decimal import Decimal\n\n"
            "def clamp_margin(raw_rate: float) -> Decimal:\n"
            "    return Decimal(raw_rate)  # type: ignore[arg-type]\n",
            encoding="utf-8",
        )
        (fx / "py_good/pricing/margin_solver.py").write_text(
            "from decimal import Decimal\n\n"
            "DOC_NOTE = 'avoid # type: ignore suppressions on Decimal conversions'\n\n"
            "def clamp_margin(raw_rate: float) -> Decimal:\n"
            "    return Decimal(f'{raw_rate:.6f}')\n",
            encoding="utf-8",
        )
        _, py_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="type:python-type-ignore-decimal",
            title="Suppressed mypy error with # type: ignore instead of converting float",
            what="Added # type: ignore[arg-type] in pricing/margin_solver.py.",
            bad_dir=fx / "py_bad",
            good_dir=fx / "py_good",
        )
        self.assertEqual(py_rec["layer"], "type")

        # 2. TypeScript `@ts-ignore` / `as any` escape
        (fx / "ts_bad/ui/checkout").mkdir(parents=True)
        (fx / "ts_good/ui/checkout").mkdir(parents=True)
        (fx / "ts_bad/ui/checkout/CurrencyBadge.tsx").write_text(
            "export function formatMinorUnits(amount: unknown): string {\n"
            "  // @ts-ignore\n"
            "  return (amount as any).toFixed(0);\n"
            "}\n",
            encoding="utf-8",
        )
        (fx / "ts_good/ui/checkout/CurrencyBadge.tsx").write_text(
            "export function formatMinorUnits(amount: number): string {\n"
            "  return Math.round(amount).toString();\n"
            "}\n",
            encoding="utf-8",
        )
        _, ts_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="type:ts-ignore-currency-badge",
            title="Used @ts-ignore and as any in CurrencyBadge formatter",
            what="Bypassed strict TypeScript check in ui/checkout/CurrencyBadge.tsx.",
            bad_dir=fx / "ts_bad",
            good_dir=fx / "ts_good",
        )
        self.assertEqual(ts_rec["layer"], "type")

    def test_lint_layer_ast_kwarg_bare_except_mutable_default_forbidden_file_and_yaml_config(self):
        root = scratch_adopter()
        fx = Path(tempfile.mkdtemp(prefix="mil-fx-lint-"))
        self.addCleanup(shutil.rmtree, fx, True)

        # 1. `subprocess.run(..., shell=True)` vs `subprocess.run(..., shell=False)`
        (fx / "kw_bad/deploy").mkdir(parents=True)
        (fx / "kw_good/deploy").mkdir(parents=True)
        (fx / "kw_bad/deploy/release_runner.py").write_text(
            "import subprocess\n\n"
            "def publish_tag(tag_name: str) -> None:\n"
            "    subprocess.run(f'git push origin {tag_name}', shell=True, check=True)\n",
            encoding="utf-8",
        )
        (fx / "kw_good/deploy/release_runner.py").write_text(
            "import subprocess\n\n"
            "def publish_tag(tag_name: str) -> None:\n"
            "    subprocess.run(['git', 'push', 'origin', tag_name], shell=False, check=True)\n",
            encoding="utf-8",
        )
        _, kw_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="lint:subprocess-shell-true",
            title="Invoked subprocess.run with shell=True in release runner",
            what="Used shell=True with interpolated tag_name in deploy/release_runner.py.",
            bad_dir=fx / "kw_bad",
            good_dir=fx / "kw_good",
        )
        self.assertEqual(kw_rec["layer"], "lint")

        # 2. Bare `except:` vs typed `except TimeoutError:`
        (fx / "ex_bad/workers").mkdir(parents=True)
        (fx / "ex_good/workers").mkdir(parents=True)
        (fx / "ex_bad/workers/webhook_sender.py").write_text(
            "def deliver_event(send_fn, payload: dict) -> bool:\n"
            "    try:\n"
            "        send_fn(payload)\n"
            "        return True\n"
            "    except:\n"
            "        return False\n",
            encoding="utf-8",
        )
        (fx / "ex_good/workers/webhook_sender.py").write_text(
            "def deliver_event(send_fn, payload: dict) -> bool:\n"
            "    try:\n"
            "        send_fn(payload)\n"
            "        return True\n"
            "    except TimeoutError:\n"
            "        return False\n",
            encoding="utf-8",
        )
        _, ex_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="lint:bare-except-swallows-cancelled",
            title="Bare except swallowed KeyboardInterrupt and SystemExit in webhook sender",
            what="Used bare `except:` instead of catching TimeoutError.",
            bad_dir=fx / "ex_bad",
            good_dir=fx / "ex_good",
        )
        self.assertEqual(ex_rec["layer"], "lint")

        # 3. Mutable default argument `headers: dict = {}`
        (fx / "md_bad/http_client").mkdir(parents=True)
        (fx / "md_good/http_client").mkdir(parents=True)
        (fx / "md_bad/http_client/request_builder.py").write_text(
            "def build_headers(correlation_id: str, extra: dict[str, str] = {}) -> dict[str, str]:\n"
            "    extra['X-Correlation-ID'] = correlation_id\n"
            "    return extra\n",
            encoding="utf-8",
        )
        (fx / "md_good/http_client/request_builder.py").write_text(
            "def build_headers(correlation_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:\n"
            "    merged = dict(extra) if extra else {}\n"
            "    merged['X-Correlation-ID'] = correlation_id\n"
            "    return merged\n",
            encoding="utf-8",
        )
        _, md_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="lint:mutable-default-headers",
            title="Shared mutable dict default leaked correlation IDs across requests",
            what="Defined `extra: dict[str, str] = {}` in build_headers.",
            bad_dir=fx / "md_bad",
            good_dir=fx / "md_good",
        )
        self.assertEqual(md_rec["layer"], "lint")

        # 4. Forbidden extra file `config/.env.production`
        (fx / "fp_bad/config").mkdir(parents=True)
        (fx / "fp_good/config").mkdir(parents=True)
        (fx / "fp_bad/config/app.yaml").write_text("service: ledger-api\n", encoding="utf-8")
        (fx / "fp_good/config/app.yaml").write_text("service: ledger-api\n", encoding="utf-8")
        (fx / "fp_bad/config/.env.production").write_text("STRIPE_WEBHOOK_SECRET=whsec_redacted\n", encoding="utf-8")
        _, fp_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="lint:committed-production-dotenv",
            title="Committed config/.env.production into the repository",
            what="Left config/.env.production in the working tree.",
            bad_dir=fx / "fp_bad",
            good_dir=fx / "fp_good",
        )
        self.assertEqual(fp_rec["layer"], "lint")

        # 5. YAML config scalar drift (`tls.verify_peer: false` vs `true`)
        (fx / "cfg_bad/config").mkdir(parents=True)
        (fx / "cfg_good/config").mkdir(parents=True)
        (fx / "cfg_bad/config/gateway.yaml").write_text(
            "upstream:\n  host: api.internal\ntls:\n  verify_peer: false\n",
            encoding="utf-8",
        )
        (fx / "cfg_good/config/gateway.yaml").write_text(
            "upstream:\n  host: api.internal\ntls:\n  verify_peer: true\n",
            encoding="utf-8",
        )
        _, cfg_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="lint:disabled-tls-verify-peer",
            title="Disabled tls.verify_peer in gateway configuration",
            what="Set tls.verify_peer to false to work around a staging certificate mismatch.",
            bad_dir=fx / "cfg_bad",
            good_dir=fx / "cfg_good",
        )
        self.assertEqual(cfg_rec["layer"], "lint")

    def test_ci_diff_scope_allowlist_and_companion_cochange(self):
        root = scratch_adopter()
        fx = Path(tempfile.mkdtemp(prefix="mil-fx-diff-"))
        self.addCleanup(shutil.rmtree, fx, True)

        # 1. PR scope allowlist (`pr.yaml` + `base/` & `head/`)
        for side in ("scope_bad", "scope_good"):
            (fx / side / "base/catalog").mkdir(parents=True)
            (fx / side / "base/security").mkdir(parents=True)
            (fx / side / "head/catalog").mkdir(parents=True)
            (fx / side / "head/security").mkdir(parents=True)
            (fx / side / "pr.yaml").write_text("kind: catalog-sync\nbranch: sync/q4-tariffs\n", encoding="utf-8")
            (fx / side / "base/catalog/tariffs.yaml").write_text("region: JP\nrate_bps: 1000\n", encoding="utf-8")
            (fx / side / "head/catalog/tariffs.yaml").write_text("region: JP\nrate_bps: 1050\n", encoding="utf-8")
            (fx / side / "base/security/rbac.yaml").write_text("admin_roles:\n  - secops\n", encoding="utf-8")
        (fx / "scope_bad/head/security/rbac.yaml").write_text(
            "admin_roles:\n  - secops\n  - sync-bot\n",
            encoding="utf-8",
        )
        (fx / "scope_good/head/security/rbac.yaml").write_text("admin_roles:\n  - secops\n", encoding="utf-8")

        _, scope_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="scope:catalog-sync-edited-rbac",
            title="Catalog-sync PR modified security/rbac.yaml outside catalog/",
            what="A catalog-sync PR expanded admin_roles in security/rbac.yaml.",
            bad_dir=fx / "scope_bad",
            good_dir=fx / "scope_good",
        )
        self.assertEqual(scope_rec["layer"], "ci")
        scope_meta = extract_metadata(root / scope_rec["check"]["path"])
        self.assertEqual(scope_meta["input"], "diff")

        # 2. Companion file co-change (`schema/ledger_entry.sql` requires `migrations/manifest.json`)
        for side in ("co_bad", "co_good"):
            (fx / side / "base/schema").mkdir(parents=True)
            (fx / side / "base/migrations").mkdir(parents=True)
            (fx / side / "head/schema").mkdir(parents=True)
            (fx / side / "head/migrations").mkdir(parents=True)
            (fx / side / "base/schema/ledger_entry.sql").write_text(
                "CREATE TABLE ledger_entry (id BIGINT PRIMARY KEY, amount_minor BIGINT NOT NULL);\n",
                encoding="utf-8",
            )
            (fx / side / "head/schema/ledger_entry.sql").write_text(
                "CREATE TABLE ledger_entry (id BIGINT PRIMARY KEY, amount_minor BIGINT NOT NULL, currency CHAR(3) NOT NULL);\n",
                encoding="utf-8",
            )
            (fx / side / "base/migrations/manifest.json").write_text(
                json.dumps({"latest_revision": "20261001_init"}, indent=2) + "\n",
                encoding="utf-8",
            )
        (fx / "co_bad/head/migrations/manifest.json").write_text(
            json.dumps({"latest_revision": "20261001_init"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (fx / "co_good/head/migrations/manifest.json").write_text(
            json.dumps({"latest_revision": "20261123_add_currency"}, indent=2) + "\n",
            encoding="utf-8",
        )

        _, co_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="scope:schema-without-migration-manifest",
            title="Changed schema/ledger_entry.sql without updating migrations/manifest.json",
            what="Edited SQL schema directly without bumping migrations/manifest.json.",
            bad_dir=fx / "co_bad",
            good_dir=fx / "co_good",
        )
        self.assertEqual(co_rec["layer"], "ci")

    def test_hook_action_command_and_protected_path_drafts(self):
        root = scratch_adopter()
        fx = Path(tempfile.mkdtemp(prefix="mil-fx-hook-"))
        self.addCleanup(shutil.rmtree, fx, True)

        # 1. Destructive shell command (`terraform destroy -auto-approve`)
        (fx / "cmd_bad").mkdir(parents=True)
        (fx / "cmd_good").mkdir(parents=True)
        (fx / "cmd_bad/action.json").write_text(
            json.dumps({"kind": "command", "command": "terraform destroy -auto-approve -target=module.ledger_db"}) + "\n",
            encoding="utf-8",
        )
        (fx / "cmd_good/action.json").write_text(
            json.dumps({"kind": "command", "command": "terraform plan -target=module.ledger_db"}) + "\n",
            encoding="utf-8",
        )
        _, cmd_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="boot:terraform-destroy-auto-approve",
            title="Ran terraform destroy -auto-approve against stateful database module",
            what="Agent executed terraform destroy -auto-approve during troubleshooting.",
            bad_dir=fx / "cmd_bad",
            good_dir=fx / "cmd_good",
        )
        self.assertEqual(cmd_rec["layer"], "hook")
        self.assertEqual(cmd_rec["check"]["tool"], "hook")

        # 2. Deleting an immutable historical migration (`kind: delete`)
        (fx / "path_bad").mkdir(parents=True)
        (fx / "path_good").mkdir(parents=True)
        (fx / "path_bad/action.json").write_text(
            json.dumps({"kind": "delete", "path": "migrations/0001_initial_ledger.sql"}) + "\n",
            encoding="utf-8",
        )
        (fx / "path_good/action.json").write_text(
            json.dumps({"kind": "write", "path": "migrations/0009_invoice_index.sql"}) + "\n",
            encoding="utf-8",
        )
        _, path_rec = _record_auto_and_prove(
            self,
            root,
            fingerprint="boot:deleted-applied-migration",
            title="Deleted already-applied migration 0001_initial_ledger.sql",
            what="Agent deleted an applied migration file instead of adding a forward migration.",
            bad_dir=fx / "path_bad",
            good_dir=fx / "path_good",
        )
        self.assertEqual(path_rec["layer"], "hook")

    def test_verify_property_fallback_for_behavioral_multi_variant_bug(self):
        root = scratch_adopter()
        fx = Path(tempfile.mkdtemp(prefix="mil-fx-verify-"))
        self.addCleanup(shutil.rmtree, fx, True)

        # Multi-variant numerical bug with identical imports, function names, and AST node kinds:
        # `int(q * len(xs))` overflows on `q == 1.0` vs `min(len(xs) - 1, int(q * len(xs)))`
        for vname, expr in (
            ("bad/real/analytics", "sorted_vals[int(q * len(sorted_vals))]"),
            ("bad/near-upper-bound/analytics", "sorted_vals[int(round(q * len(sorted_vals)))]"),
            ("good/real/analytics", "sorted_vals[min(len(sorted_vals) - 1, max(0, int(q * len(sorted_vals))))]"),
            ("good/singleton/analytics", "sorted_vals[0] if len(sorted_vals) == 1 else sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]"),
        ):
            (fx / vname).mkdir(parents=True)
            (fx / vname / "quantiles.py").write_text(
                "def empirical_quantile(samples: list[float], q: float) -> float:\n"
                "    sorted_vals = sorted(samples)\n"
                f"    return {expr}\n",
                encoding="utf-8",
            )

        rec_proc = run_mil(
            "record",
            "--root",
            str(root),
            "--fingerprint",
            "data:quantile-upper-bound-index-error",
            "--title",
            "Quantile estimator raised IndexError at q=1.0 boundary",
            "--what",
            "empirical_quantile indexed len(sorted_vals) when q == 1.0.",
            "--source",
            "CI failure",
            "--actor",
            "cloud-agent",
            "--reporter",
            "ci",
            "--bad-variant",
            f"real={fx / 'bad/real'}",
            "--bad-variant",
            f"near-upper-bound={fx / 'bad/near-upper-bound'}",
            "--good-variant",
            f"real={fx / 'good/real'}",
            "--good-variant",
            f"singleton={fx / 'good/singleton'}",
            "--auto",
        )
        self.assertEqual(rec_proc.returncode, 0, rec_proc.stdout + rec_proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        rec = load_record_file(rec_path)
        self.assertEqual(rec["layer"], "verify")
        self.assertEqual(rec["classification"]["kind"], "data")

        # `mil scaffold` must NOT emit a false-confidence regex; it emits a seeded MIL_SEED template exiting 2
        scaf = run_mil("scaffold", rec["id"], "--root", str(root))
        self.assertEqual(scaf.returncode, 0, scaf.stdout + scaf.stderr)
        self.assertIn("scaffolded .make-it-lint/checks/", scaf.stdout)
        rec = load_record_file(rec_path)
        check_text = (root / rec["check"]["path"]).read_text(encoding="utf-8")
        self.assertIn("MIL_SEED", check_text)
        self.assertIn("analytics/quantiles.py", check_text)
        self.assertIn("return 2", check_text)
        unedited = run_mil("prove", rec["id"], "--root", str(root))
        self.assertEqual(unedited.returncode, 1)

    def test_external_linter_detection_sg_shadow_utils_safety_and_ast_grep_bundle(self):
        fake_bin = Path(tempfile.mkdtemp(prefix="mil-fake-bin-"))
        self.addCleanup(shutil.rmtree, fake_bin, True)

        # 1. Linux `/usr/bin/sg` (shadow-utils) must NEVER be misidentified as `ast-grep`
        fake_sg = fake_bin / "sg"
        fake_sg.write_text("#!/bin/sh\necho 'sg (shadow-utils) 4.13'\nexit 0\n", encoding="utf-8")
        fake_sg.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = str(fake_bin)
            tc_shadow = detect_toolchain(scratch_adopter())
            self.assertIsNone(tc_shadow.ast_grep_bin)
        finally:
            os.environ["PATH"] = old_path

        # 2. Real/simulated `ast-grep` binary + `<dir>/{check.py, rule.yml}` bundle + PreToolUse guard
        fake_ag = fake_bin / "ast-grep"
        fake_ag.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "from pathlib import Path\n"
            "if '--version' in sys.argv:\n"
            "    print('ast-grep 0.30.0')\n"
            "    raise SystemExit(0)\n"
            "rule_idx = sys.argv.index('--rule') + 1\n"
            "rule_text = Path(sys.argv[rule_idx]).read_text(encoding='utf-8')\n"
            "pat = ''\n"
            "for ln in rule_text.splitlines():\n"
            "    if ln.strip().startswith('pattern:'):\n"
            "        pat = json.loads(ln.split(':', 1)[1].strip())\n"
            "scan_root = Path(sys.argv[-1])\n"
            "results = []\n"
            "for p in sorted(scan_root.rglob('*.js')):\n"
            "    for idx, line in enumerate(p.read_text(encoding='utf-8').splitlines()):\n"
            "        if pat and pat in line:\n"
            "            results.append({'file': str(p), 'range': {'start': {'line': idx}}, 'message': 'disallow eval()'})\n"
            "print(json.dumps(results))\n"
            "raise SystemExit(1 if results else 0)\n",
            encoding="utf-8",
        )
        fake_ag.chmod(fake_ag.stat().st_mode | stat.S_IXUSR)

        root = scratch_adopter()
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "dev@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Dev"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "chore: init"], check=True)

        fx = Path(tempfile.mkdtemp(prefix="mil-fx-ag-"))
        self.addCleanup(shutil.rmtree, fx, True)
        (fx / "bad/web").mkdir(parents=True)
        (fx / "good/web").mkdir(parents=True)
        (fx / "bad/web/expression_filter.js").write_text(
            "export function runFilter(expr, row) {\n  return eval(expr);\n}\n",
            encoding="utf-8",
        )
        (fx / "good/web/expression_filter.js").write_text(
            "export function runFilter(field, row) {\n  return row[field];\n}\n",
            encoding="utf-8",
        )

        env_with_ag = {"PATH": str(fake_bin) + os.pathsep + old_path}
        rec_proc = run_mil(
            "record",
            "--root",
            str(root),
            "--fingerprint",
            "lint:js-eval-filter",
            "--title",
            "Used eval() in expression_filter.js",
            "--what",
            "Evaluated user filter string with eval(expr).",
            "--source",
            "security review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "@secops",
            "--bad",
            str(fx / "bad"),
            "--good",
            str(fx / "good"),
            "--auto",
            env=env_with_ag,
        )
        self.assertEqual(rec_proc.returncode, 0, rec_proc.stdout + rec_proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        inc_id = load_record_file(rec_path)["id"]

        # When `.ast-grep` / `sgconfig.yml` is present in the repo, `mil scaffold` picks `ast-grep` automatically!
        (root / "sgconfig.yml").write_text("ruleDirs:\n  - rules\n", encoding="utf-8")
        scaf = run_mil("scaffold", inc_id, "--root", str(root), env=env_with_ag)
        self.assertEqual(scaf.returncode, 0, scaf.stdout + scaf.stderr)
        self.assertIn("tool=ast-grep", scaf.stdout)
        bundle_dir = root / ".make-it-lint/checks/js_eval_filter"
        self.assertTrue((bundle_dir / "check.py").is_file())
        self.assertTrue((bundle_dir / "rule.yml").is_file())

        # While `state == "open"` and uncommitted, `rule.yml` inside the bundle is editable
        self.assertIsNone(
            check_protected_write(root, ".make-it-lint/checks/js_eval_filter/rule.yml", kind="write")
        )

        lint_proc = run_mil("lint", "--root", str(root), env=env_with_ag)
        self.assertEqual(lint_proc.returncode, 0, lint_proc.stdout + lint_proc.stderr)
        prove_proc = run_mil("prove", inc_id, "--root", str(root), env=env_with_ag)
        self.assertEqual(prove_proc.returncode, 0, prove_proc.stdout + prove_proc.stderr)

        # Once `state == "proven"`, `rule.yml` is locked against silent agent edits
        self.assertIsNotNone(
            check_protected_write(root, ".make-it-lint/checks/js_eval_filter/rule.yml", kind="write")
        )
        # Creating a brand-new file inside an already-proven bundle directory is ALSO blocked
        self.assertIsNotNone(
            check_protected_write(root, ".make-it-lint/checks/js_eval_filter/check.aa", kind="write")
        )

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_git_bad_ref_good_ref_workflow_and_deleted_file_edge_case(self):
        root = scratch_adopter()
        git = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)
        git("init", "-q")
        git("config", "user.email", "dev@example.com")
        git("config", "user.name", "Dev")

        (root / "src").mkdir()
        (root / "src/reconciler.py").write_text(
            "def reconcile_settlements(entries: list[int]) -> int:\n"
            "    breakpoint()\n"
            "    return sum(entries)\n",
            encoding="utf-8",
        )
        git("add", "-A")
        git("commit", "-q", "-m", "feat: add settlement reconciler (accidentally left breakpoint)")

        (root / "src/reconciler.py").write_text(
            "def reconcile_settlements(entries: list[int]) -> int:\n"
            "    return sum(entries)\n",
            encoding="utf-8",
        )
        git("add", "src/reconciler.py")
        git("commit", "-q", "-m", "fix: remove leftover breakpoint from settlement reconciler")

        rec_proc = run_mil(
            "record",
            "--root",
            str(root),
            "--fingerprint",
            "lint:reconciler-breakpoint",
            "--title",
            "Left breakpoint() in settlement reconciler",
            "--what",
            "Committed breakpoint() in src/reconciler.py.",
            "--source",
            "PR review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "@reviewer",
            "--bad-ref",
            "HEAD~1",
            "--good-ref",
            "HEAD",
            "--auto",
        )
        self.assertEqual(rec_proc.returncode, 0, rec_proc.stdout + rec_proc.stderr)
        self.assertIn("bad/ <- HEAD~1: src/reconciler.py", rec_proc.stdout)
        self.assertIn("good/ <- HEAD: src/reconciler.py", rec_proc.stdout)

        inc1 = sorted(p.parent.name for p in (root / ".make-it-lint/ledger").glob("F-*/record.yaml"))[-1]
        self.assertEqual(run_mil("scaffold", inc1, "--root", str(root)).returncode, 0)
        prove1 = run_mil("prove", inc1, "--check-hunks", "--root", str(root))
        self.assertEqual(prove1.returncode, 0, prove1.stdout + prove1.stderr)
        self.assertNotIn("missed hunk", prove1.stdout + prove1.stderr)

        # Edge case: commit adds a forbidden `.env.staging` file and next commit deletes ONLY that file.
        # `_copy_from_git_refs` must include a shared context file so `good/` is not empty!
        (root / "config").mkdir()
        (root / "config/.env.staging").write_text("LEDGER_DB_PASSWORD=redacted_staging_pw\n", encoding="utf-8")
        git("add", "config/.env.staging")
        git("commit", "-q", "-m", "chore: accidentally add .env.staging")
        git("rm", "-q", "config/.env.staging")
        git("commit", "-q", "-m", "fix: remove .env.staging from git")

        # Also test `mil record` (unclassified) -> `mil scaffold --auto` one-step auto-classify + draft!
        rec2 = run_mil(
            "record",
            "--root",
            str(root),
            "--fingerprint",
            "lint:staging-dotenv-committed",
            "--title",
            "Committed config/.env.staging to git",
            "--what",
            "Staged and committed config/.env.staging.",
            "--source",
            "PR review",
            "--actor",
            "cloud-agent",
            "--reporter",
            "@reviewer",
            "--bad-ref",
            "HEAD~1",
            "--good-ref",
            "HEAD",
        )
        self.assertEqual(rec2.returncode, 0, rec2.stdout + rec2.stderr)
        inc2 = sorted(p.parent.name for p in (root / ".make-it-lint/ledger").glob("F-*/record.yaml"))[-1]
        scaf2 = run_mil("scaffold", inc2, "--auto", "--root", str(root))
        self.assertEqual(scaf2.returncode, 0, scaf2.stdout + scaf2.stderr)
        self.assertIn("auto-selected: kind=lint", scaf2.stdout)
        self.assertIn("strategy=tree_forbidden_path", scaf2.stdout)
        prove2 = run_mil("prove", inc2, "--root", str(root))
        self.assertEqual(prove2.returncode, 0, prove2.stdout + prove2.stderr)
        lint_final = run_mil("lint", "--root", str(root))
        self.assertEqual(lint_final.returncode, 0, lint_final.stdout + lint_final.stderr)

    def test_classify_auto_and_semgrep_bundle(self):
        root = scratch_adopter()
        fx = Path(tempfile.mkdtemp(prefix="mil-fx-cls-"))
        self.addCleanup(shutil.rmtree, fx, True)
        ci_log = fx / "ci.log"
        ci_log.write_text(
            "2026-11-23T04:12:00Z\truff\t1\tsrc/auth_session.py:19: error: eval() is forbidden in session decoder\n",
            encoding="utf-8",
        )
        (fx / "bad/src").mkdir(parents=True)
        (fx / "good/src").mkdir(parents=True)
        (fx / "bad/src/auth_session.py").write_text(
            "import json\n\n"
            "def decode_claims(raw: str) -> dict:\n"
            "    return eval(raw)\n",
            encoding="utf-8",
        )
        (fx / "good/src/auth_session.py").write_text(
            "import json\n\n"
            "def decode_claims(raw: str) -> dict:\n"
            "    return json.loads(raw)\n",
            encoding="utf-8",
        )

        rec_proc = run_mil(
            "record",
            "--root",
            str(root),
            "--from-ci",
            str(ci_log),
            "--bad",
            str(fx / "bad"),
            "--good",
            str(fx / "good"),
        )
        self.assertEqual(rec_proc.returncode, 0, rec_proc.stdout + rec_proc.stderr)
        rec_path = next((root / ".make-it-lint/ledger").glob("F-*/record.yaml"))
        inc_id = load_record_file(rec_path)["id"]

        cls_proc = run_mil("classify", inc_id, "--auto", "--root", str(root))
        self.assertEqual(cls_proc.returncode, 0, cls_proc.stdout + cls_proc.stderr)
        self.assertIn("auto-selected: kind=lint decidable=yes layer=lint", cls_proc.stdout)

        # Explicit `--tool semgrep` produces a valid `<dir>/{check.py, rule.yml}` bundle
        scaf = run_mil("scaffold", inc_id, "--tool", "semgrep", "--root", str(root))
        self.assertEqual(scaf.returncode, 0, scaf.stdout + scaf.stderr)
        rec_now = load_record_file(rec_path)
        bundle = root / rec_now["check"]["path"]
        self.assertTrue((bundle / "check.py").is_file())
        self.assertTrue((bundle / "rule.yml").is_file())
        self.assertIn("rules:", (bundle / "rule.yml").read_text(encoding="utf-8"))
        lint = run_mil("lint", "--root", str(root))
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_product_tree_ignores_venv_node_modules_and_scopes_architecture_boundary_and_py_launcher(self):
        from tests.support import MIL, ROOT, run_mil_tty

        root = scratch_adopter()
        fx = Path(tempfile.mkdtemp(prefix="mil-fx-prod-"))
        self.addCleanup(shutil.rmtree, fx, True)
        (fx / "bad/billing/domain").mkdir(parents=True)
        (fx / "good/billing/domain").mkdir(parents=True)
        (fx / "bad/billing/domain/invoice_calculator.py").write_text(
            "from billing.infra.stripe_gateway import charge_card\n",
            encoding="utf-8",
        )
        (fx / "good/billing/domain/invoice_calculator.py").write_text(
            "from billing.domain.ports import PaymentPort\n",
            encoding="utf-8",
        )
        inc_id, _ = _record_auto_and_prove(
            self,
            root,
            fingerprint="architecture:domain-stripe-import",
            title="Domain imported stripe_gateway directly",
            what="billing/domain imported billing.infra.stripe_gateway.",
            bad_dir=fx / "bad",
            good_dir=fx / "good",
        )

        # Populate live working tree with:
        # 1) `.venv/` containing third-party code importing `billing.infra.stripe_gateway`
        # 2) `billing/infra/webhook_controller.py` legitimately importing `billing.infra.stripe_gateway`
        # 3) clean `billing/domain/invoice_calculator.py`
        (root / ".venv/lib/site-packages").mkdir(parents=True)
        (root / ".venv/lib/site-packages/vendor_sdk.py").write_text(
            "from billing.infra.stripe_gateway import charge_card\n",
            encoding="utf-8",
        )
        (root / "billing/infra").mkdir(parents=True)
        (root / "billing/infra/webhook_controller.py").write_text(
            "from billing.infra.stripe_gateway import verify_signature\n",
            encoding="utf-8",
        )
        (root / "billing/domain").mkdir(parents=True)
        (root / "billing/domain/invoice_calculator.py").write_text(
            "from billing.domain.ports import PaymentPort\n",
            encoding="utf-8",
        )

        # Promote all the way to `block`: `_require_green_on_tree` must pass with 0 false positives!
        for step in ("shadow", "comment", "block"):
            p = run_mil_tty(
                f"{step}\n",
                "promote",
                inc_id,
                "--to",
                step,
                "--approved-by",
                "@architect",
                "--root",
                str(root),
            )
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

        ci_clean = run_mil("ci", "--root", str(root))
        self.assertEqual(ci_clean.returncode, 0, ci_clean.stdout + ci_clean.stderr)

        # Introducing the violation under `billing/domain/` triggers `block`
        (root / "billing/domain/settlement.py").write_text(
            "from billing.infra.stripe_gateway import transfer_payout\n",
            encoding="utf-8",
        )
        ci_red = run_mil("ci", "--root", str(root))
        self.assertEqual(ci_red.returncode, 1, ci_red.stdout + ci_red.stderr)
        self.assertIn("billing/domain/settlement.py:1", ci_red.stdout)

        # Verify `bin/mil` POSIX wrapper falls back to `py -3` when `python3`/`python` are absent (Windows Git Bash case)
        if shutil.which("sh"):
            py_only_bin = Path(tempfile.mkdtemp(prefix="mil-py-launcher-"))
            self.addCleanup(shutil.rmtree, py_only_bin, True)
            import sys as _sys

            fake_py = py_only_bin / "py"
            fake_py.write_text(
                f'#!/bin/sh\nif [ "${{1:-}}" = "-3" ]; then shift; fi\nexec "{_sys.executable}" "$@"\n',
                encoding="utf-8",
            )
            fake_py.chmod(0o755)
            # Symlink minimal POSIX tools needed by `bin/mil` (`dirname`, `uname`) without `python` or `python3`
            for util in ("dirname", "uname"):
                src_u = shutil.which(util)
                if src_u:
                    os.symlink(src_u, py_only_bin / util)
            sh_bin = shutil.which("sh")
            assert sh_bin is not None
            proc_py = subprocess.run(
                [sh_bin, str(MIL), "--version"],
                cwd=str(ROOT),
                env={"PATH": str(py_only_bin)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc_py.returncode, 0, proc_py.stdout + proc_py.stderr)
            self.assertIn("make-it-lint/v1", proc_py.stdout)


