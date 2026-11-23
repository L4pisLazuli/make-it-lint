from __future__ import annotations

import unittest

from tests.support import ROOT


class ReadmeInstallTests(unittest.TestCase):
    def test_readme_documents_license_and_license_exists(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("MIT license", text)
        self.assertIn("LICENSE", text)
        self.assertTrue((ROOT / "LICENSE").is_file())
