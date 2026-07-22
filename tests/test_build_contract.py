from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import build_site


class BuildContractTests(unittest.TestCase):
    def test_package_status_uses_exact_registry_value(self) -> None:
        statuses = {"unit": "外部レビュー済", "unit--child": "人間レビュー済"}
        self.assertEqual(build_site.package_status("unit", statuses), "外部レビュー済")

    def test_package_status_aggregates_or_uses_conservative_child(self) -> None:
        self.assertEqual(
            build_site.package_status(
                "unit",
                {"unit--a": "人間レビュー済", "unit--b": "人間レビュー済"},
            ),
            "人間レビュー済",
        )
        self.assertEqual(
            build_site.package_status(
                "unit",
                {"unit--a": "人間レビュー済", "unit--b": "ドラフト"},
            ),
            "ドラフト",
        )

    def test_subject_readme_controls_unit_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            subject = source / "materials" / "jhs-math-1"
            (subject / "jhs-math-1-second").mkdir(parents=True)
            (subject / "jhs-math-1-first").mkdir()
            (subject / "README.md").write_text(
                "[Parent](../README.md)\n"
                "[Second](jhs-math-1-second/README.md)\n"
                "[First](jhs-math-1-first/README.md)\n",
                encoding="utf-8",
            )
            orders = build_site.subject_unit_orders_from_source(source)
        self.assertEqual(
            orders["jhs-math-1"],
            ["jhs-math-1-second", "jhs-math-1-first"],
        )

    def test_svg_css_escape_cannot_hide_external_url(self) -> None:
        payload = r'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0" fill="u\72l(https://evil.example/p.svg#p)"/></svg>'
        with self.assertRaises(build_site.BuildError):
            build_site.validate_svg_source(payload, Path("escaped.svg"))

    def test_svg_internal_paint_server_remains_allowed(self) -> None:
        payload = '<svg xmlns="http://www.w3.org/2000/svg"><defs><pattern id="p"/></defs><path d="M0 0" fill="url(#p)"/></svg>'
        build_site.validate_svg_source(payload, Path("internal.svg"))

    def test_review_state_conflict_is_conservative(self) -> None:
        self.assertTrue(build_site.has_review_state_conflict("候補ドラフト", "人間レビュー済"))
        self.assertFalse(build_site.has_review_state_conflict("候補ドラフト", "外部レビュー済"))

    def test_public_html_count_ignores_nested_staging_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "index.html").write_text("top", encoding="utf-8")
            (root / "about").mkdir()
            (root / "about" / "index.html").write_text("about", encoding="utf-8")
            (root / "site-output").mkdir()
            (root / "site-output" / "stale.html").write_text("stale", encoding="utf-8")
            html = [
                path.relative_to(root).as_posix()
                for path in build_site.iter_public_files(root)
                if path.suffix == ".html"
            ]
        self.assertEqual(html, ["about/index.html", "index.html"])


if __name__ == "__main__":
    unittest.main()
