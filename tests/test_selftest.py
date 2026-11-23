from __future__ import annotations

import builtins
import symtable
import unittest
from pathlib import Path

from tests.support import ROOT, SAMPLE, copy_tree, run_mil

_BUILTINS = set(dir(builtins)) | {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__spec__",
    "__annotations__",
}


def _unresolved_symbols(st: symtable.SymbolTable, path: Path, enclosing: set[str]) -> list[str]:
    visible = set(enclosing) | {
        sym.get_name()
        for sym in st.get_symbols()
        if sym.is_local() or sym.is_imported() or sym.is_parameter()
    }
    found: list[str] = []
    for sym in st.get_symbols():
        name = sym.get_name()
        if sym.is_global() and not sym.is_assigned() and not sym.is_imported():
            if name not in visible and name not in _BUILTINS:
                found.append(f"{path.relative_to(ROOT).as_posix()}:{st.get_lineno()} ({st.get_name()}): {name}")
    child_enclosing = visible if st.get_type() != "class" else set(enclosing)
    for child in st.get_children():
        found.extend(_unresolved_symbols(child, path, child_enclosing))
    return found


class MutationSelftest(unittest.TestCase):
    def test_weakened_scope_check_fails_prove(self):
        repo = copy_tree(SAMPLE)
        check = repo / ".make-it-lint/checks/scope_paths.py"
        original = check.read_text(encoding="utf-8")
        # Keep leading metadata so the file is still a check; drop detection.
        lines = original.splitlines(keepends=True)
        out = []
        fences = 0
        for line in lines:
            out.append(line)
            if line.startswith("# ---"):
                fences += 1
                if fences == 2:
                    out.append("raise SystemExit(0)\n")
                    break
        check.write_text("".join(out), encoding="utf-8")
        proc = run_mil("prove", "F-20260311-01", "--root", str(repo))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("green-on-bad, green-on-good", proc.stdout)

    def test_unweakened_scope_check_still_proves(self):
        proc = run_mil("prove", "F-20260311-01", "--root", str(SAMPLE))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("red-on-bad, green-on-good", proc.stdout)

    def test_no_python_file_has_unresolved_symbols(self):
        unresolved: list[str] = []
        for path in sorted(ROOT.rglob("*.py")):
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            st = symtable.symtable(source, str(path), "exec")
            top_defined = {sym.get_name() for sym in st.get_symbols() if sym.is_local() or sym.is_imported()}
            unresolved.extend(_unresolved_symbols(st, path, top_defined))
        self.assertEqual(unresolved, [], "Unresolved symbols found:\n" + "\n".join(unresolved))
