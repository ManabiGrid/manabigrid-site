from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_svg_ids_do_not_depend_on_checkout_root(self) -> None:
        doc = Path("materials/jhs-math-1/example/lesson_01.md")
        asset = Path("materials/jhs-math-1/example/assets/figure.svg")
        first = build_site.svg_id_prefix(doc, asset, 1)
        second = build_site.svg_id_prefix(doc, asset, 1)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^mg-[0-9a-f]{10}-$")

    def test_svg_viewbox_layout_detects_compact_panorama(self) -> None:
        self.assertEqual(build_site.svg_viewbox_layout("0 0 480 130"), (True, True))
        self.assertEqual(build_site.svg_viewbox_layout("0 0 640 480"), (True, False))
        self.assertEqual(build_site.svg_viewbox_layout("0 0 320 240"), (False, False))
        self.assertEqual(build_site.svg_viewbox_layout("not a viewbox"), (False, False))

    def test_page_shell_has_theme_end_navigation_and_conditional_figure_dialog(self) -> None:
        ordinary = build_site.page(
            Path("about/index.html"),
            "案内",
            "説明",
            "<p>本文</p>",
            [("トップ", Path("index.html")), ("案内", None)],
            "page-about",
        )
        self.assertIn('id="page-top"', ordinary)
        self.assertIn("data-theme-select", ordinary)
        self.assertIn("_assets/theme.js", ordinary)
        self.assertIn('class="page-end-nav', ordinary)
        self.assertNotIn("data-figure-dialog", ordinary)

        figure_page = build_site.page(
            Path("content/example.html"),
            "図のある教材",
            "説明",
            '<a class="figure-source" data-figure-open href="../_media/example.svg">図を大きく見る</a>',
            [("トップ", Path("index.html")), ("教材", None)],
            "page-lesson",
        )
        self.assertEqual(figure_page.count("data-figure-dialog "), 1)
        self.assertIn("data-figure-close", figure_page)
        self.assertIn("data-figure-original", figure_page)

    def test_search_console_verification_meta_is_home_only(self) -> None:
        home = build_site.page(
            Path("index.html"),
            "トップ",
            "説明",
            "<p>本文</p>",
            [("トップ", None)],
            "page-home",
        )
        lesson = build_site.page(
            Path("content/example.html"),
            "教材",
            "説明",
            "<p>本文</p>",
            [("トップ", Path("index.html")), ("教材", None)],
            "page-lesson",
        )
        expected = (
            '<meta name="google-site-verification" '
            f'content="{build_site.SITE_CONFIG["google_site_verification"]}">'
        )
        self.assertEqual(home.count(expected), 1)
        self.assertNotIn('name="google-site-verification"', lesson)

    def test_figure_dialog_script_has_keyboard_and_backdrop_close_contract(self) -> None:
        script = (
            (Path(build_site.__file__).with_name("static") / "site.js").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("event.key !== 'Escape'", script)
        self.assertIn("event.target === dialog", script)
        self.assertIn("activeLink.focus({ preventScroll: true })", script)
        self.assertIn("window.scrollTo(0, openingScrollY)", script)

    def test_print_theme_restores_primary_controls_to_black_on_white(self) -> None:
        css = (
            (Path(build_site.__file__).with_name("static") / "site.css").read_text(
                encoding="utf-8"
            )
        )
        print_css = css.split("@media print", 1)[1]
        self.assertRegex(
            print_css,
            r"\.button,\s*\.primary-action\s*\{[^}]*background:\s*#fff\s*!important;"
            r"[^}]*color:\s*#000\s*!important;",
        )

    def test_unnumbered_unit_resources_use_the_full_content_column(self) -> None:
        doc = build_site.Doc(
            path=Path("/source/answer.md"),
            rel=Path("materials/example/answer.md"),
            output=Path("content/materials/example/answer.html"),
            kind="answer",
            title="解答集",
            subject="jhs-math-1",
            unit="example",
            sha256="0" * 64,
            frontmatter=False,
            tags=0,
        )
        current = Path("units/example/index.html")
        numbered = build_site.resource(current, doc, 1)
        plain = build_site.resource(current, doc)
        css = (
            (Path(build_site.__file__).with_name("static") / "site.css").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn('class="resource-item-numbered"', numbered)
        self.assertIn('class="resource-item-plain"', plain)
        self.assertNotIn('class="resource-number"', plain)
        self.assertRegex(
            css,
            r"\.resource-list li\.resource-item-plain\s*\{"
            r"[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);",
        )

    def test_empty_unit_resources_use_the_same_plain_row_contract(self) -> None:
        body = build_site.unit_body(
            build_site.Unit(
                slug="jhs-math-3-empty",
                subject="jhs-math-3",
                title="空の単元",
                docs=[],
            )
        )
        self.assertEqual(body.count('class="resource-item-plain"'), 3)
        self.assertEqual(
            body.count('<li class="resource-item-plain"><div>'),
            3,
        )
        self.assertNotRegex(body, r"<(?:ol|ul)><li>(?:独立|案内)")

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

    def test_home_learning_grid_includes_available_and_preparing_families(self) -> None:
        units = {
            "jhs-math-1-sample": build_site.Unit(
                slug="jhs-math-1-sample",
                subject="jhs-math-1",
                title="sample",
                docs=[],
            )
        }
        available_spec = build_site.CurriculumFamilySpec(
            "jhs-math", "中学", "数学", "jhs-math-", "jhs-math"
        )
        preparing_spec = build_site.CurriculumFamilySpec(
            "hs-eng", "高校", "英語", "hs-eng-", "hs-eng"
        )
        families = [
            build_site.CurriculumFamily(
                available_spec,
                [
                    build_site.CurriculumItem(
                        "jhs-math-sample", "例", "数学", "中1", "人間レビュー済", "公開コア", "unit"
                    )
                ],
                ["jhs-math-1"],
                1,
                0,
            ),
            build_site.CurriculumFamily(
                preparing_spec,
                [
                    build_site.CurriculumItem(
                        "hs-eng-sample", "例", "英語", "高1", "未着手", "公開コア", "unit"
                    )
                ],
                [],
                0,
                0,
            ),
        ]
        body = build_site.home_body(
            units,
            [],
            families,
        )
        self.assertEqual(body.count('class="curriculum-grid-item '), 2)
        self.assertIn('data-family="jhs-math"', body)
        self.assertIn('data-family="hs-eng"', body)
        self.assertIn('data-availability="available"', body)
        self.assertIn('data-availability="preparing"', body)
        self.assertIn("準備中", body)
        self.assertIn('href="curriculum/hs-eng/index.html"', body)
        self.assertIn('href="subjects/jhs-math-1/index.html"', body)
        self.assertNotIn("OPEN LEARNING / 中学3年 数学", body)
        self.assertNotIn("中3数学の8単元", body)
        self.assertNotIn('id="math3-route"', body)
        self.assertNotIn("中3数学の診断", body)
        self.assertIn("言葉やつまずきから探したい", body)
        self.assertIn("OPEN LEARNING / 2 CURRICULUM ENTRANCES", body)
        self.assertNotIn("AVAILABLE ENTRANCES", body)

    def test_curriculum_contract_rejects_changed_school_label(self) -> None:
        raw = json.loads(build_site.CURRICULUM_CONTRACT_PATH.read_text(encoding="utf-8"))
        raw["families"][0]["school"] = "大学"
        with tempfile.TemporaryDirectory() as temporary:
            contract = Path(temporary) / "contract.json"
            contract.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with patch.object(build_site, "CURRICULUM_CONTRACT_PATH", contract):
                with self.assertRaisesRegex(build_site.BuildError, "固定10入口"):
                    build_site.load_curriculum_contract()

    def test_source_checkout_rejects_dirty_canonical_tree(self) -> None:
        with patch.object(
            build_site,
            "git",
            side_effect=lambda _source, *args: (
                " M curriculum/PROGRESS_INDEX.md"
                if args[:2] == ("status", "--porcelain")
                else "https://github.com/ManabiGrid/manabigrid.git"
            ),
        ):
            with self.assertRaisesRegex(build_site.BuildError, "未commit差分"):
                build_site.validate_source_checkout(Path("/canonical"))

    def test_source_checkout_rejects_noncanonical_origin(self) -> None:
        with patch.object(
            build_site,
            "git",
            side_effect=lambda _source, *args: (
                "" if args[:2] == ("status", "--porcelain") else "https://github.com/example/fork.git"
            ),
        ):
            with self.assertRaisesRegex(build_site.BuildError, "正本origin"):
                build_site.validate_source_checkout(Path("/canonical"))

    def test_source_checkout_rejects_clean_unpushed_head(self) -> None:
        command_map = {
            ("status", "--porcelain", "--untracked-files=all"): "",
            ("config", "--get", "remote.origin.url"): "https://github.com/ManabiGrid/manabigrid.git",
            ("rev-parse", "HEAD"): "b" * 40,
            ("rev-parse", "refs/remotes/origin/main"): "a" * 40,
        }
        with patch.object(
            build_site,
            "git",
            side_effect=lambda _source, *args: command_map[args],
        ):
            with self.assertRaisesRegex(build_site.BuildError, "公式main"):
                build_site.validate_source_checkout(Path("/canonical"))

    @staticmethod
    def write_progress_fixture(root: Path, status: str = "未着手", duplicate: bool = False) -> None:
        (root / "curriculum").mkdir()
        module_id = "jhs-math-sample" if duplicate else "jhs-math-module"
        (root / "curriculum/PROGRESS_INDEX.md").write_text(
            "## 全単元一覧（unit_id 順）\n\n"
            "| unit_id | 単元名 | 科目 | 学校段階・学年 | レーン | 状態 |\n"
            "|---|---|---|---|---|---|\n"
            f"| `jhs-math-sample` | 例 | 数学 | 中1 | 公開コア | **{status}** |\n\n"
            "## 科目モジュール（単元と別枠: 診断・巻末資料）\n\n"
            "| module_id | 名称 | 科目 | 学校段階・学年 | 状態 |\n"
            "|---|---|---|---|---|\n"
            f"| `{module_id}` | 診断 | 数学 | 中学 | **未着手** |\n",
            encoding="utf-8",
        )

    def test_curriculum_parser_reads_only_canonical_flat_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_progress_fixture(root)
            units, modules = build_site.parse_curriculum_progress(
                root, ("未着手", "人間レビュー済")
            )
        self.assertEqual([item.item_id for item in units], ["jhs-math-sample"])
        self.assertEqual([item.item_id for item in modules], ["jhs-math-module"])

    def test_curriculum_parser_rejects_unknown_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_progress_fixture(root, status="制作中")
            with self.assertRaises(build_site.BuildError):
                build_site.parse_curriculum_progress(root, ("未着手", "人間レビュー済"))

    def test_curriculum_parser_rejects_duplicate_id_across_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_progress_fixture(root, duplicate=True)
            with self.assertRaises(build_site.BuildError):
                build_site.parse_curriculum_progress(root, ("未着手", "人間レビュー済"))

    def test_curriculum_parser_rejects_malformed_data_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_progress_fixture(root)
            progress = root / "curriculum/PROGRESS_INDEX.md"
            progress.write_text(
                progress.read_text(encoding="utf-8").replace(
                    "| `jhs-math-sample` |", "| jhs-math-sample |"
                ),
                encoding="utf-8",
            )
            with self.assertRaises(build_site.BuildError):
                build_site.parse_curriculum_progress(root, ("未着手", "人間レビュー済"))

    def test_curriculum_parser_rejects_indented_data_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_progress_fixture(root)
            progress = root / "curriculum/PROGRESS_INDEX.md"
            progress.write_text(
                progress.read_text(encoding="utf-8").replace(
                    "| `jhs-math-sample` |", "  | `jhs-math-sample` |"
                ),
                encoding="utf-8",
            )
            with self.assertRaises(build_site.BuildError):
                build_site.parse_curriculum_progress(root, ("未着手", "人間レビュー済"))

    def test_curriculum_parser_requires_separator_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_progress_fixture(root)
            progress = root / "curriculum/PROGRESS_INDEX.md"
            progress.write_text(
                progress.read_text(encoding="utf-8").replace(
                    "|---|---|---|---|---|---|\n", "", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaises(build_site.BuildError):
                build_site.parse_curriculum_progress(root, ("未着手", "人間レビュー済"))

    def test_curriculum_family_join_rejects_unknown_prefix(self) -> None:
        spec = build_site.CurriculumFamilySpec(
            "jhs-math", "中学", "数学", "jhs-math-", "jhs-math"
        )
        item = build_site.CurriculumItem(
            "elementary-math-sample", "例", "数学", "小1", "未着手", "公開コア", "unit"
        )
        with self.assertRaises(build_site.BuildError):
            build_site.collect_curriculum_families(Path("."), {}, [spec], [item])


if __name__ == "__main__":
    unittest.main()
