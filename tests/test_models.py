from __future__ import annotations

import unittest

from mil.models import (
    LAYERS,
    allowed_next,
    is_proven_result,
    parse_hit_line,
    stronger_layer,
    transitions_ok,
)


class TransitionTests(unittest.TestCase):
    def test_forward_and_demote_chains(self):
        self.assertIn("proven", allowed_next("open"))
        self.assertIn("human_review", allowed_next("open"))
        self.assertIn("open", allowed_next("human_review"))
        self.assertIn("shadow", allowed_next("proven"))
        self.assertIn("comment", allowed_next("shadow"))
        self.assertIn("block", allowed_next("comment"))
        # Demote chain
        self.assertIn("comment", allowed_next("block"))
        self.assertIn("shadow", allowed_next("comment"))
        self.assertIn("proven", allowed_next("shadow"))
        # Skipping refused
        self.assertNotIn("block", allowed_next("proven"))
        self.assertNotIn("shadow", allowed_next("open"))

    def test_history_cannot_skip_rollout_states(self):
        self.assertIsNone(transitions_ok(["open", "proven", "shadow", "comment", "block"]))
        self.assertIsNone(transitions_ok(["open", "proven", "shadow", "proven"]))
        self.assertIn("illegal", transitions_ok(["open", "shadow"]) or "")
        self.assertIn("start at open or human_review", transitions_ok(["proven"]) or "")

    def test_stop_at_open_or_human_review_is_ok(self):
        self.assertIsNone(transitions_ok(["open"]))
        self.assertIsNone(transitions_ok(["human_review"]))
        self.assertIsNone(transitions_ok(["open", "human_review", "open", "proven"]))

    def test_stronger_layer_moves_toward_architecture_without_doc(self):
        self.assertNotIn("doc", LAYERS)
        self.assertEqual(stronger_layer("ci"), "hook")
        self.assertEqual(stronger_layer("hook"), "verify")
        self.assertEqual(stronger_layer("lint"), "type")
        self.assertIsNone(stronger_layer("architecture"))

    def test_is_proven_result(self):
        self.assertTrue(is_proven_result("red-on-bad, green-on-good"))
        self.assertTrue(is_proven_result("red-on-bad 2/2, green-on-good 2/2"))
        self.assertTrue(is_proven_result("red-on-bad 3/3, green-on-good 1/1"))
        self.assertFalse(is_proven_result("red-on-bad 1/2, green-on-good 2/2"))
        self.assertFalse(is_proven_result("green-on-bad, green-on-good"))
        self.assertFalse(is_proven_result(None))

    def test_parse_hit_line_including_windows_drive_paths(self):
        self.assertEqual(
            parse_hit_line("config/runner.yaml: collect may not change this path"),
            ("config/runner.yaml", None, "collect may not change this path"),
        )
        self.assertEqual(
            parse_hit_line("tests/test_totals.py:12: baseline updated with the test"),
            ("tests/test_totals.py", 12, "baseline updated with the test"),
        )
        self.assertEqual(
            parse_hit_line(r"C:\repo\inventory\items.yaml:18: negative stock count"),
            (r"C:\repo\inventory\items.yaml", 18, "negative stock count"),
        )
        self.assertIsNone(parse_hit_line("not a hit"))
