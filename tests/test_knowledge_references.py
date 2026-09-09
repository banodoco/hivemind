from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executors.references import parse_references  # noqa: E402


class KnowledgeReferenceParserTests(unittest.TestCase):
    def test_closed_forms_and_labelled_destination(self) -> None:
        refs = parse_references(
            "[resource:42] [resource:42@7] [message:123@456] "
            "[revision:7] [evidence:8] [guide](hivemind:resource:42@7)"
        )
        self.assertEqual(
            refs,
            [
                {"target_kind": "resource", "target_id": "42", "target_version_id": None, "labelled": "false"},
                {"target_kind": "resource", "target_id": "42", "target_version_id": "7", "labelled": "false"},
                {"target_kind": "message", "target_id": "123", "target_version_id": "456", "labelled": "false"},
                {"target_kind": "revision", "target_id": "7", "target_version_id": None, "labelled": "false"},
                {"target_kind": "evidence", "target_id": "8", "target_version_id": None, "labelled": "false"},
            ],
        )

    def test_code_fence_inline_and_escape_exclusions(self) -> None:
        text = r"\[resource:1] `[message:2]`\n```\n[evidence:3]\n```\n[resource:4]"
        refs = parse_references(text)
        self.assertEqual([r["target_id"] for r in refs], ["4"])

    def test_double_backtick_code_span_is_excluded(self) -> None:
        self.assertEqual(parse_references("``[resource:42]`` [message:7]"), [
            {"target_kind": "message", "target_id": "7", "target_version_id": None, "labelled": "false"},
        ])

    def test_ordinary_links_unknown_kinds_and_bad_ids_are_not_hivemind_refs(self) -> None:
        refs = parse_references(
            "[resource:0] [thing:1] [resource:x] "
            "[ordinary](https://example.test/resource:3)"
        )
        self.assertEqual(refs, [])

    def test_version_is_rejected_for_unversionable_target(self) -> None:
        with self.assertRaises(ValueError):
            parse_references("[revision:7@2]")
        with self.assertRaises(ValueError):
            parse_references("[evidence:8@2]")


if __name__ == "__main__":
    unittest.main()
