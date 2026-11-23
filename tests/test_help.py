from __future__ import annotations

import unittest

from tests.support import (
    materialize_help_paths,
    parse_help_examples,
    run_mil,
    scratch_adopter,
)

COMMANDS = (
    "record",
    "classify",
    "scaffold",
    "link",
    "prove",
    "promote",
    "demote",
    "retire",
    "status",
    "lint",
    "ci",
    "guard",
    "events",
)


class HelpExampleTests(unittest.TestCase):
    def test_every_help_example_exits_0_in_a_temp_repo(self):
        seen = 0
        for command in COMMANDS:
            proc = run_mil(command, "--help")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            examples = parse_help_examples(proc.stdout)
            if command == "record":
                self.assertGreaterEqual(len(examples), 1, proc.stdout)
            for argv in examples:
                seen += 1
                root = scratch_adopter()
                materialize_help_paths(root, argv)
                ran = run_mil(*argv, cwd=root)
                self.assertEqual(
                    ran.returncode,
                    0,
                    f"{command} example {argv!r}\nstdout:\n{ran.stdout}\nstderr:\n{ran.stderr}",
                )
        self.assertGreaterEqual(seen, 1)

    def test_record_help_example_includes_required_flags(self):
        text = run_mil("record", "--help").stdout
        examples = parse_help_examples(text)
        self.assertGreaterEqual(len(examples), 1, text)
        first = examples[0]
        self.assertIn("--fingerprint", first)
        self.assertIn("--title", first)
        self.assertIn("--source", first)
        self.assertIn("--actor", first)
        self.assertIn("--reporter", first)
        self.assertIn("--what", first)
        self.assertNotIn("/tmp/failing-diff", text)
