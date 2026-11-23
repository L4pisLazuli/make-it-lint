from __future__ import annotations

import json
import unittest

from mil import CONTRACT
from mil.ledger import load_record_file
from mil.schema import contract_from_config_schema, load_schema, validate
from tests.support import ROOT


class ContractVersionTests(unittest.TestCase):
    def test_python_schema_and_docs_agree(self):
        self.assertEqual(CONTRACT, "make-it-lint/v1")
        self.assertEqual(contract_from_config_schema(), CONTRACT)
        docs = (ROOT / "docs" / "contract.md").read_text(encoding="utf-8")
        self.assertIn(CONTRACT, docs)
        self.assertTrue(docs.startswith("# make-it-lint/v1"))
        self.assertFalse((ROOT / "lib" / "mil" / "contract.md").exists())
        self.assertFalse((ROOT / "lib" / "mil" / "embedded.py").exists())
        self.assertFalse((ROOT / "tests" / "embed_schemas.py").exists())
        schema_path = ROOT / "schema" / "config.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["contract"]["const"], CONTRACT)

    def test_schemas_are_objects(self):
        for name in (
            "incident.schema.json",
            "waiver.schema.json",
            "check-meta.schema.json",
            "config.schema.json",
        ):
            schema = load_schema(name)
            self.assertIn(schema["type"], {"object", "array"}, name)
            self.assertTrue((ROOT / "schema" / name).is_file())

    def test_sample_records_validate_against_schema(self):
        inc_schema = load_schema("incident.schema.json")
        for rec_path in sorted((ROOT / "examples/sample-repo/.make-it-lint/ledger").glob("F-*/record.yaml")):
            rec = load_record_file(rec_path)
            issues = validate(rec, inc_schema)
            self.assertEqual(issues, [], f"{rec_path.parent.name}: {[str(i) for i in issues]}")
