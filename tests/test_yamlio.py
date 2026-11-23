from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mil import yamlio
from mil.ledger import load_record_file, save_record_file
from tests.support import SAMPLE

SCOPE_RECORD = SAMPLE / ".make-it-lint/ledger/F-20260311-01/record.yaml"


class YamlSubsetTests(unittest.TestCase):
    def test_roundtrip_scope_record(self):
        original = load_record_file(SCOPE_RECORD)
        dumped = yamlio.dump(original)
        again = yamlio.load(dumped)
        self.assertEqual(again["id"], "F-20260311-01")
        self.assertEqual(again["classification"]["decidable"], "yes")
        self.assertIsInstance(again["classification"]["decidable"], str)
        self.assertNotIn("hits", again["enforcement"])
        self.assertIn("config/runner.yaml", again["what_happened"])
        self.assertEqual(again["history"][0]["state"], "open")
        self.assertEqual(again["check"]["id"], "MIL-SCOPE-001")

    def test_all_sample_records_load(self):
        ledger = SAMPLE / ".make-it-lint/ledger"
        ids = []
        for rec in sorted(ledger.glob("F-*/record.yaml")):
            data = load_record_file(rec)
            ids.append(data["id"])
            self.assertEqual(data["id"], rec.parent.name)
        self.assertEqual(
            ids,
            [
                "F-20260311-01",
                "F-20260402-01",
                "F-20260518-01",
                "F-20260609-01",
                "F-20260722-01",
                "F-20260814-01",
                "F-20260905-01",
            ],
        )

    def test_flexible_indent_and_unicode_escapes(self):
        text = (
            "history:\n"
            "  - state: open\n"
            "    at: 2026-03-11\n"
            "  -   state: proven\n"
            "      at: 2026-03-12\n"
            'venue: "caf\\u00e9 \\U0001F6A8 \\x41"\n'
        )
        data = yamlio.load(text)
        self.assertEqual(data["history"][1]["state"], "proven")
        self.assertEqual(data["venue"], "café 🚨 A")

    def test_rejects_anchors_aliases_tags_merge_keys_and_bad_escapes(self):
        for bad_yaml in (
            "default_owner: &lead \"@ops-lead\"\nowner: *lead\n",
            "owner: *lead\n",
            "rule: !regex \"^collect/\"\n",
            "merged:\n  <<: *base\n",
            "flow: [*lead]\n",
            "flow_merge: {<<: base}\n",
            'note: "unknown \\q escape"\n',
            'note: "short \\u00e"\n',
            'note: "surrogate \\uD800"\n',
            "what_happened: unquoted first line\n  indented second line\n",
        ):
            with self.subTest(bad_yaml=bad_yaml):
                with self.assertRaises(yamlio.YAMLError):
                    yamlio.load(bad_yaml)

        quoted = yamlio.load('anchor_lit: "&lead"\nalias_lit: "*lead"\n"<<": "shift"\n')
        self.assertEqual(quoted, {"anchor_lit": "&lead", "alias_lit": "*lead", "<<": "shift"})

    def test_inline_nested_sequences_and_control_char_roundtrip(self):
        nested = yamlio.load(
            "batches:\n"
            "  - - SKU-101\n"
            "    - SKU-102\n"
            "  - - SKU-204\n"
        )
        self.assertEqual(nested, {"batches": [["SKU-101", "SKU-102"], ["SKU-204"]]})
        self.assertEqual(yamlio.load(yamlio.dump(nested)), nested)

        ctrl = {"payload": "nul=\0 esc=\x1b sep=\u2028"}
        self.assertEqual(yamlio.load(yamlio.dump(ctrl)), ctrl)

    def test_flow_mapping_history(self):
        text = (
            "history:\n"
            "  - {state: open, at: 2026-03-11}\n"
            "  - {state: proven, at: 2026-03-12}\n"
        )
        data = yamlio.load(text)
        self.assertEqual(data["history"][1]["state"], "proven")
        self.assertEqual(data["history"][1]["at"], "2026-03-12")

    def test_flow_sequence_paths(self):
        text = 'paths: ["config/runner.yaml", "inventory/items.yaml"]\n'
        data = yamlio.load(text)
        self.assertEqual(data["paths"][0], "config/runner.yaml")

    def test_literal_and_folded_blocks(self):
        lit = yamlio.load("what_happened: |\n  line one\n  line two\n")
        self.assertEqual(lit["what_happened"], "line one\nline two\n")
        folded = yamlio.load("message: >\n  Collect may only change inventory/\n  and data/collected/.\n")
        self.assertIn("inventory/", folded["message"])
        self.assertNotIn("\nCollect", folded["message"])

    def test_yes_is_string_not_bool_and_duplicate_keys_rejected(self):
        self.assertEqual(yamlio.load("decidable: yes\n")["decidable"], "yes")
        with self.assertRaises(yamlio.YAMLError):
            yamlio.load("id: F-20260311-01\nid: F-20260311-02\n")
        with self.assertRaises(yamlio.YAMLError):
            yamlio.load("id:\tF-20260311-01\n")

    def test_dump_then_reload_save_record(self):
        rec = load_record_file(SCOPE_RECORD)
        rec["proof"]["last_result"] = "red-on-bad, green-on-good"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            tmp = Path(fh.name)
        save_record_file(tmp, rec)
        again = load_record_file(tmp)
        tmp.unlink(missing_ok=True)
        self.assertEqual(again["proof"]["last_result"], "red-on-bad, green-on-good")
        self.assertEqual(again["owner"], "@ledger-owner")

    def test_strings_and_numbers_roundtrip_exactly(self):
        values = [
            "3.10",
            "1e5",
            "-12",
            "0",
            "-0",
            "null",
            "true",
            "inventory/items.yaml\nconfig/runner.yaml\n",
            "  indented log line\nnext\n",
            "key: value # tail",
        ]
        for value in values:
            for doc in ({"k": value}, {"k": [value]}, {"k": [{"n": value}]}):
                again = yamlio.load(yamlio.dump(doc))
                self.assertEqual(again, doc, repr(value))
        nums = {"min_qty": -12, "zero": 0, "max_qty": 24, "ratio": 0.25, "tol": 1e-05}
        self.assertEqual(yamlio.load(yamlio.dump(nums)), nums)
