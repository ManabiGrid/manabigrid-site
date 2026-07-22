from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import check_site


class CheckSiteContractTests(unittest.TestCase):
    def test_review_conflicts_are_derived_from_canonical_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "curriculum").mkdir()
            (source / "curriculum/PROGRESS_INDEX.md").write_text(
                "| 教科 | 単元 | `jhs-math-2-example` | 人間レビュー済 |\n",
                encoding="utf-8",
            )
            unit = source / "materials/jhs-math-2/jhs-math-2-example"
            unit.mkdir(parents=True)
            (unit / "lesson.md").write_text("候補ドラフト（人間レビュー前）", encoding="utf-8")
            expected = check_site.expected_review_state_conflicts(source)
        self.assertEqual(
            expected,
            {
                "materials/jhs-math-2/jhs-math-2-example/lesson.md": "人間レビュー済"
            },
        )

    def test_empty_self_report_cannot_hide_expected_conflict(self) -> None:
        expected = {"materials/jhs-math-2/example/lesson.md": "人間レビュー済"}
        self.assertFalse(check_site.review_state_contract_matches(expected, [], set()))


if __name__ == "__main__":
    unittest.main()
