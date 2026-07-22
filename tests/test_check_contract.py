from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path, PurePosixPath

import check_site


class CheckSiteContractTests(unittest.TestCase):
    @staticmethod
    def write_minimal_progress(
        root: Path,
        unit_row: str = "| `jhs-math-sample` | 例 | 数学 | 中1 | 公開コア | **未着手** |",
        include_unit_separator: bool = True,
    ) -> None:
        (root / "curriculum").mkdir()
        unit_separator = "|---|---|---|---|---|---|\n" if include_unit_separator else ""
        (root / "curriculum/PROGRESS_INDEX.md").write_text(
            "## 全単元一覧（unit_id 順）\n\n"
            "| unit_id | 単元名 | 科目 | 学校段階・学年 | レーン | 状態 |\n"
            + unit_separator
            + unit_row
            + "\n\n## 科目モジュール（単元と別枠: 診断・巻末資料）\n\n"
            "| module_id | 名称 | 科目 | 学校段階・学年 | 状態 |\n"
            "|---|---|---|---|---|\n"
            "| `jhs-math-module` | 診断 | 数学 | 中学 | **未着手** |\n",
            encoding="utf-8",
        )

    def test_canonical_curriculum_snapshot_rejects_changed_school_label(self) -> None:
        contract_path = Path(check_site.__file__).with_name("curriculum_grid.contract.json")
        raw = json.loads(contract_path.read_text(encoding="utf-8"))
        raw["families"][0]["school"] = "大学"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changed_contract = root / "contract.json"
            changed_contract.write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "fixed expectations"):
                check_site.canonical_curriculum_snapshot(root, changed_contract)

    def test_source_checkout_contract_rejects_dirty_or_forked_source(self) -> None:
        source_info = {
            "repository": "https://github.com/ManabiGrid/manabigrid",
            "origin": "https://github.com/ManabiGrid/manabigrid.git",
            "git_status_before": "",
            "git_status_after": "",
        }
        self.assertEqual(
            check_site.source_checkout_contract_errors(
                source_info,
                actual_status="",
                actual_origin="https://github.com/ManabiGrid/manabigrid.git",
                actual_head="a" * 40,
                expected_head="a" * 40,
            ),
            [],
        )
        errors = check_site.source_checkout_contract_errors(
            source_info,
            actual_status=" M curriculum/PROGRESS_INDEX.md",
            actual_origin="https://github.com/example/fork.git",
            actual_head="b" * 40,
            expected_head="a" * 40,
        )
        self.assertTrue(any("uncommitted" in error for error in errors))
        self.assertTrue(any("official canonical" in error for error in errors))
        self.assertTrue(any("verified official main" in error for error in errors))

    def test_publication_site_commit_must_match_expected_release(self) -> None:
        report = {"publication": {"site_commit": "a" * 40}}
        self.assertEqual(
            check_site.publication_site_contract_errors(report, "a" * 40), []
        )
        errors = check_site.publication_site_contract_errors(report, "b" * 40)
        self.assertTrue(any("expected release commit" in error for error in errors))

    def test_curriculum_status_summary_uses_visible_contract_separator(self) -> None:
        self.assertEqual(
            check_site.curriculum_status_summary_from_items(
                [
                    {"status": "未着手"},
                    {"status": "未着手"},
                    {"status": "外部レビュー済"},
                ]
            ),
            "未着手 2、外部レビュー済 1",
        )

    def test_canonical_curriculum_snapshot_rejects_malformed_data_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_minimal_progress(
                root,
                unit_row="| jhs-math-bad | 例 | 数学 | 中1 | 公開コア | **未着手** |",
            )
            contract = Path(check_site.__file__).with_name("curriculum_grid.contract.json")
            with self.assertRaisesRegex(ValueError, "malformed row"):
                check_site.canonical_curriculum_snapshot(root, contract)

    def test_canonical_curriculum_snapshot_rejects_indented_data_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_minimal_progress(
                root,
                unit_row="  | `jhs-math-sample` | 例 | 数学 | 中1 | 公開コア | **未着手** |",
            )
            contract = Path(check_site.__file__).with_name("curriculum_grid.contract.json")
            with self.assertRaisesRegex(ValueError, "indented row"):
                check_site.canonical_curriculum_snapshot(root, contract)

    def test_canonical_curriculum_snapshot_requires_separator_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_minimal_progress(root, include_unit_separator=False)
            contract = Path(check_site.__file__).with_name("curriculum_grid.contract.json")
            with self.assertRaisesRegex(ValueError, "header missing before rows"):
                check_site.canonical_curriculum_snapshot(root, contract)

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

    def test_home_grid_counts_are_derived_from_canonical_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            first = source / "materials/jhs-test/jhs-test-first"
            second = source / "materials/jhs-test/jhs-test-second"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "lesson_01.md").write_text("# lesson", encoding="utf-8")
            (first / "answer_key.md").write_text("# answer", encoding="utf-8")
            (first / "notes.md").write_text("# reference", encoding="utf-8")
            (second / "diagnostic.md").write_text("# diagnostic", encoding="utf-8")
            (second / "student_textbook_print.md").write_text(
                "# textbook", encoding="utf-8"
            )
            counts = check_site.canonical_home_grid_counts(source)
        self.assertEqual(counts, {"jhs-test": (2, 3)})

    @staticmethod
    def home_grid_fixture() -> tuple[dict[str, tuple[int, int]], dict, str]:
        canonical = {"jhs-a": (2, 3), "jhs-b": (1, 1)}
        report = {
            "source": "subject_registry_and_collected_units",
            "subjects": [
                {"slug": "jhs-a", "label": "A", "packages": 2, "lesson_pages": 3},
                {"slug": "jhs-b", "label": "B", "packages": 1, "lesson_pages": 1},
            ],
            "total_packages": 3,
            "total_lesson_pages": 4,
        }
        html = """
<ul class="learning-grid" role="list">
  <li class="learning-grid-item" data-subject="jhs-a" data-package-count="2" data-lesson-page-count="3">
    <a href="subjects/jhs-a/index.html" aria-label="Aを開く。2パッケージ、レッスン・本文3ページ"><span class="learning-grid-heading"><strong>A</strong></span><span class="learning-grid-slot"></span><span class="learning-grid-slot"></span></a>
  </li>
  <li class="learning-grid-item" data-subject="jhs-b" data-package-count="1" data-lesson-page-count="1">
    <a href="subjects/jhs-b/index.html" aria-label="Bを開く。1パッケージ、レッスン・本文1ページ"><span class="learning-grid-heading"><strong>B</strong></span><span class="learning-grid-slot"></span></a>
  </li>
</ul>
"""
        return canonical, report, html

    def test_home_grid_contract_accepts_source_exact_report_and_html(self) -> None:
        canonical, report, html = self.home_grid_fixture()
        self.assertEqual(check_site.home_grid_contract_errors(canonical, report, html), [])

    def test_home_grid_contract_rejects_canonical_omission(self) -> None:
        canonical, report, html = self.home_grid_fixture()
        report["subjects"] = report["subjects"][:1]
        self.assertTrue(
            any(
                "omits canonical subjects" in error
                for error in check_site.home_grid_contract_errors(canonical, report, html)
            )
        )

    def test_home_grid_contract_rejects_duplicate_subject(self) -> None:
        canonical, report, html = self.home_grid_fixture()
        report["subjects"].append(dict(report["subjects"][0]))
        self.assertTrue(
            any(
                "duplicate subjects" in error
                for error in check_site.home_grid_contract_errors(canonical, report, html)
            )
        )

    def test_home_grid_contract_rejects_wrong_count(self) -> None:
        canonical, report, html = self.home_grid_fixture()
        report["subjects"][0]["lesson_pages"] = 99
        self.assertTrue(
            any(
                "count differs from canonical source" in error
                for error in check_site.home_grid_contract_errors(canonical, report, html)
            )
        )

    def test_home_grid_contract_rejects_wrong_href(self) -> None:
        canonical, report, html = self.home_grid_fixture()
        html = html.replace(
            'href="subjects/jhs-a/index.html"', 'href="subjects/jhs-b/index.html"', 1
        )
        self.assertTrue(
            any(
                "subject href mismatch" in error
                for error in check_site.home_grid_contract_errors(canonical, report, html)
            )
        )

    def test_home_grid_contract_rejects_wrong_visible_label(self) -> None:
        canonical, report, html = self.home_grid_fixture()
        html = html.replace("<strong>A</strong>", "<strong>教材</strong>")
        self.assertTrue(
            any(
                "visible label mismatch" in error
                for error in check_site.home_grid_contract_errors(canonical, report, html)
            )
        )

    def test_home_grid_contract_rejects_wrong_aria_label(self) -> None:
        canonical, report, html = self.home_grid_fixture()
        html = html.replace(
            "Aを開く。2パッケージ、レッスン・本文3ページ",
            "教材を開く",
        )
        self.assertTrue(
            any(
                "aria-label mismatch" in error
                for error in check_site.home_grid_contract_errors(canonical, report, html)
            )
        )

    @staticmethod
    def curriculum_grid_fixture() -> tuple[dict, dict, str]:
        family = {
            "slug": "hs-eng",
            "unit_id_prefix": "hs-eng-",
            "school": "高校",
            "subject": "英語",
            "label": "高校 英語",
            "href": "curriculum/hs-eng/index.html",
            "registered_units": 2,
            "status_counts": {"人間レビュー済": 2},
            "material_subjects": [],
            "packages": 0,
            "lesson_pages": 0,
            "availability": "preparing",
        }
        snapshot = {
            "contract_version": 1,
            "progress_index_sha256": "abc",
            "families": [family],
            "units": [
                {
                    "id": "hs-eng-first", "title": "第一", "subject": "英語",
                    "grade": "高1", "status": "人間レビュー済",
                },
                {
                    "id": "hs-eng-second", "title": "第二", "subject": "英語",
                    "grade": "高1", "status": "人間レビュー済",
                },
            ],
            "modules": [
                {
                    "id": "hs-eng-module", "title": "診断", "subject": "英語",
                    "grade": "高校", "status": "未着手",
                }
            ],
            "item_material": {},
            "total_packages": 0,
            "total_lesson_pages": 0,
        }
        report = {
            "source": "curriculum_progress_index_and_canonical_materials",
            "contract_version": 1,
            "progress_index_sha256": "abc",
            "families": [
                {
                    key: family[key]
                    for key in (
                        "slug", "label", "school", "subject", "href",
                        "registered_units", "status_counts", "material_subjects",
                        "packages", "lesson_pages", "availability",
                    )
                }
            ],
            "total_entries": 1,
            "available_entries": 0,
            "preparing_entries": 1,
            "registered_units": 2,
            "registered_modules": 1,
            "total_packages": 0,
            "total_lesson_pages": 0,
        }
        html = """
<section class="home-hero"><p class="eyebrow">OPEN LEARNING / 1 CURRICULUM ENTRANCES</p><p class="hero-lead">わからなくなった場所まで戻り、自分のペースで学び直せます。中学・高校1入口のうち、0入口で0パッケージを読めます。1入口は「準備中」です。登録も記録もいりません。</p></section>
<ul class="curriculum-grid" role="list">
  <li class="curriculum-grid-item is-preparing" data-family="hs-eng" data-unit-count="2" data-package-count="0" data-lesson-page-count="0" data-availability="preparing">
    <article><div><span class="curriculum-school">高校</span><span class="curriculum-availability">準備中</span></div>
    <h3><a href="curriculum/hs-eng/index.html" aria-label="高校 英語の学習グリッドを開く。進捗表に2単元、教材は準備中">英語</a></h3>
    <p class="curriculum-count">進捗表に<strong>2単元</strong></p>
    <p class="curriculum-availability-text">このサイトで読める教材は、まだありません</p>
    <a class="curriculum-open" href="curriculum/hs-eng/index.html" aria-label="高校 英語の単元の全体像を見る">単元の全体像を見る</a></article>
  </li>
</ul>
"""
        return snapshot, report, html

    def test_curriculum_grid_accepts_preparing_entry_despite_review_status(self) -> None:
        snapshot, report, html = self.curriculum_grid_fixture()
        self.assertEqual(
            check_site.curriculum_grid_contract_errors(
                snapshot, report, html, PurePosixPath("index.html")
            ),
            [],
        )

    def test_curriculum_grid_rejects_omitted_progress_only_entry(self) -> None:
        snapshot, report, _html = self.curriculum_grid_fixture()
        errors = check_site.curriculum_grid_contract_errors(
            snapshot, report, '<ul class="curriculum-grid"></ul>', PurePosixPath("index.html")
        )
        self.assertTrue(any("membership" in error for error in errors))

    def test_curriculum_grid_rejects_false_available_state(self) -> None:
        snapshot, report, html = self.curriculum_grid_fixture()
        html = html.replace('data-availability="preparing"', 'data-availability="available"')
        self.assertTrue(
            any(
                "availability differs" in error
                for error in check_site.curriculum_grid_contract_errors(
                    snapshot, report, html, PurePosixPath("index.html")
                )
            )
        )

    def test_curriculum_grid_rejects_wrong_href(self) -> None:
        snapshot, report, html = self.curriculum_grid_fixture()
        html = html.replace("curriculum/hs-eng/index.html", "curriculum/hs-jpn/index.html")
        self.assertTrue(
            any(
                "hrefs differ" in error
                for error in check_site.curriculum_grid_contract_errors(
                    snapshot, report, html, PurePosixPath("index.html")
                )
            )
        )

    def test_curriculum_grid_rejects_wrong_aria_label(self) -> None:
        snapshot, report, html = self.curriculum_grid_fixture()
        html = html.replace("教材は準備中", "教材あり", 1)
        self.assertTrue(
            any(
                "aria-label differs" in error
                for error in check_site.curriculum_grid_contract_errors(
                    snapshot, report, html, PurePosixPath("index.html")
                )
            )
        )

    def test_curriculum_grid_rejects_repeated_open_link_name(self) -> None:
        snapshot, report, html = self.curriculum_grid_fixture()
        html = html.replace(
            ' aria-label="高校 英語の単元の全体像を見る"', "", 1
        )
        self.assertTrue(
            any(
                "open-link aria-label differs" in error
                for error in check_site.curriculum_grid_contract_errors(
                    snapshot, report, html, PurePosixPath("index.html")
                )
            )
        )

    def test_curriculum_grid_rejects_wrong_visible_count(self) -> None:
        snapshot, report, html = self.curriculum_grid_fixture()
        html = html.replace("進捗表に<strong>2単元", "進捗表に<strong>999単元")
        self.assertTrue(
            any(
                "visible unit count differs" in error
                for error in check_site.curriculum_grid_contract_errors(
                    snapshot, report, html, PurePosixPath("index.html")
                )
            )
        )

    def test_curriculum_grid_rejects_wrong_home_hero_totals(self) -> None:
        snapshot, report, html = self.curriculum_grid_fixture()
        html = html.replace("0入口で0パッケージ", "99入口で999パッケージ")
        self.assertTrue(
            any(
                "home curriculum hero totals" in error
                for error in check_site.curriculum_grid_contract_errors(
                    snapshot, report, html, PurePosixPath("index.html")
                )
            )
        )

    @staticmethod
    def curriculum_skeleton_pages(
        first_title: str = "第一", grade: str = "高1", first_material: str = "preparing"
    ) -> dict[str, str]:
        family_html = f"""
<body class="page-curriculum-family">
<section class="container unit-hero curriculum-hero">
  <div class="curriculum-title-row"><h1>高校 英語</h1><span class="curriculum-availability is-preparing">準備中</span></div>
  <p>正本の進捗表にある2単元を、学校段階・学年ごとに並べています。教材の掲載有無と制作工程の状態は別の情報です。</p>
  <p class="curriculum-hero-meta">正本の状態: 人間レビュー済 2</p>
</section>
<section class="container curriculum-preparing-note"><p class="eyebrow">NOT YET PUBLISHED</p><h2>教材は準備中です</h2><p>正本の進捗表には2単元が登録されていますが、このサイトで読める教材本文はまだありません。完成時期や制作中であることを示す表示ではありません。</p></section>
<details class="curriculum-track">
  <summary><span><strong>{grade}</strong><small>2単元</small></span><span class="track-status-summary">人間レビュー済 2</span></summary>
  <ol class="curriculum-unit-list">
    <li data-curriculum-id="hs-eng-first" data-status="人間レビュー済" data-material="{first_material}">
      <div><strong>{first_title}</strong><code>hs-eng-first</code></div><div class="curriculum-unit-state"><span class="status-chip">人間レビュー済</span><span class="curriculum-unit-pending">準備中</span></div>
    </li>
    <li data-curriculum-id="hs-eng-second" data-status="人間レビュー済" data-material="preparing">
      <div><strong>第二</strong><code>hs-eng-second</code></div><div class="curriculum-unit-state"><span class="status-chip">人間レビュー済</span><span class="curriculum-unit-pending">準備中</span></div>
    </li>
  </ol>
</details>
</body>
"""
        modules_html = """
<p>公開コア単元とは別枠の1件です。名称・学校段階・状態を正本の進捗表から表示します。</p>
<details class="curriculum-track">
  <summary><span><strong>英語</strong><small>1件</small></span><span class="track-status-summary">未着手 1</span></summary>
  <ol class="curriculum-unit-list">
    <li data-curriculum-id="hs-eng-module" data-status="未着手" data-material="preparing">
      <div><strong>診断</strong><span class="curriculum-unit-grade">高校</span><code>hs-eng-module</code></div><div class="curriculum-unit-state"><span class="status-chip">未着手</span><span class="curriculum-unit-pending">準備中</span></div>
    </li>
  </ol>
</details>
"""
        return {
            "curriculum/index.html": "<h2 id=\"curriculum-grid-title\">1の教科入口</h2><p>公開コア2単元とは別に、正本には1件の科目モジュールが登録されています。</p>",
            "curriculum/hs-eng/index.html": family_html,
            "curriculum/modules/index.html": modules_html,
        }

    def test_curriculum_skeleton_accepts_exact_visible_source_text(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        self.assertEqual(
            check_site.curriculum_skeleton_contract_errors(
                snapshot, self.curriculum_skeleton_pages()
            ),
            [],
        )

    def test_curriculum_skeleton_rejects_wrong_visible_module_grade(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        pages = self.curriculum_skeleton_pages()
        pages["curriculum/modules/index.html"] = pages[
            "curriculum/modules/index.html"
        ].replace(
            '<span class="curriculum-unit-grade">高校</span>',
            '<span class="curriculum-unit-grade">高3</span>',
        )
        errors = check_site.curriculum_skeleton_contract_errors(snapshot, pages)
        self.assertTrue(any("visible grades differ" in error for error in errors))

    def test_curriculum_skeleton_rejects_false_material_link(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        errors = check_site.curriculum_skeleton_contract_errors(
            snapshot,
            self.curriculum_skeleton_pages(first_material="available"),
        )
        self.assertTrue(any("skeleton rows differ" in error for error in errors))

    def test_curriculum_skeleton_rejects_hallucinated_visible_title(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        errors = check_site.curriculum_skeleton_contract_errors(
            snapshot,
            self.curriculum_skeleton_pages(first_title="AIが考えた架空の単元名"),
        )
        self.assertTrue(any("visible skeleton differs" in error for error in errors))

    def test_curriculum_skeleton_rejects_hallucinated_grade_group(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        errors = check_site.curriculum_skeleton_contract_errors(
            snapshot,
            self.curriculum_skeleton_pages(grade="架空の学年区分"),
        )
        self.assertTrue(any("visible skeleton differs" in error for error in errors))

    def test_curriculum_skeleton_rejects_hallucinated_completion_claim(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        pages = self.curriculum_skeleton_pages()
        pages["curriculum/hs-eng/index.html"] = pages[
            "curriculum/hs-eng/index.html"
        ].replace(
            "完成時期や制作中であることを示す表示ではありません。",
            "完成時期や制作中であることを示す表示ではありません。現在制作中で、まもなく完成予定です。",
        )
        errors = check_site.curriculum_skeleton_contract_errors(snapshot, pages)
        self.assertTrue(any("explanation missing" in error for error in errors))

    def test_curriculum_skeleton_rejects_wrong_collapsed_status_summary(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        pages = self.curriculum_skeleton_pages()
        pages["curriculum/hs-eng/index.html"] = pages[
            "curriculum/hs-eng/index.html"
        ].replace("人間レビュー済 2</span></summary>", "公開済 999</span></summary>")
        errors = check_site.curriculum_skeleton_contract_errors(snapshot, pages)
        self.assertTrue(any("visible skeleton differs" in error for error in errors))

    def test_curriculum_skeleton_rejects_wrong_visible_status_chip(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        pages = self.curriculum_skeleton_pages()
        pages["curriculum/hs-eng/index.html"] = pages[
            "curriculum/hs-eng/index.html"
        ].replace(
            '<span class="status-chip">人間レビュー済</span>',
            '<span class="status-chip">公開済</span>',
            1,
        )
        errors = check_site.curriculum_skeleton_contract_errors(snapshot, pages)
        self.assertTrue(any("visible skeleton differs" in error for error in errors))

    def test_curriculum_skeleton_rejects_wrong_visible_pending_label(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        pages = self.curriculum_skeleton_pages()
        pages["curriculum/hs-eng/index.html"] = pages[
            "curriculum/hs-eng/index.html"
        ].replace(
            '<span class="curriculum-unit-pending">準備中</span>',
            '<span class="curriculum-unit-pending">公開済</span>',
            1,
        )
        errors = check_site.curriculum_skeleton_contract_errors(snapshot, pages)
        self.assertTrue(any("visible skeleton differs" in error for error in errors))

    def available_curriculum_skeleton_fixture(self) -> tuple[dict, dict[str, str]]:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        snapshot = copy.deepcopy(snapshot)
        snapshot["families"][0]["availability"] = "available"
        snapshot["families"][0]["packages"] = 1
        snapshot["families"][0]["lesson_pages"] = 1
        snapshot["item_material"] = {"hs-eng-first": "hs-eng-package"}
        pages = self.curriculum_skeleton_pages()
        family = pages["curriculum/hs-eng/index.html"]
        family = family.replace("is-preparing\">準備中", "is-available\">教材あり", 1)
        family = re.sub(
            r'<section class="container curriculum-preparing-note">.*?</section>',
            '<section><p>1パッケージ、レッスン・本文1ページを掲載しています。</p></section>',
            family,
            count=1,
            flags=re.DOTALL,
        )
        family = family.replace('data-material="preparing"', 'data-material="available"', 1)
        family = family.replace(
            '<span class="curriculum-unit-pending">準備中</span>',
            '<a class="curriculum-unit-link" href="../../units/hs-eng-package/index.html">教材を読む</a>',
            1,
        )
        pages["curriculum/hs-eng/index.html"] = family
        return snapshot, pages

    def test_curriculum_skeleton_rejects_swapped_available_href(self) -> None:
        snapshot, pages = self.available_curriculum_skeleton_fixture()
        self.assertEqual(check_site.curriculum_skeleton_contract_errors(snapshot, pages), [])
        pages["curriculum/hs-eng/index.html"] = pages[
            "curriculum/hs-eng/index.html"
        ].replace("hs-eng-package/index.html", "another-existing-unit/index.html")
        errors = check_site.curriculum_skeleton_contract_errors(snapshot, pages)
        self.assertTrue(any("visible skeleton differs" in error for error in errors))

    def test_curriculum_skeleton_rejects_stray_row_outside_tracks(self) -> None:
        snapshot, _report, _html = self.curriculum_grid_fixture()
        pages = self.curriculum_skeleton_pages()
        pages["curriculum/hs-eng/index.html"] += (
            '<li data-curriculum-id="hs-eng-fake" data-status="公開済" '
            'data-material="available"><div><strong>架空単元</strong>'
            '<code>hs-eng-fake</code></div></li>'
        )
        errors = check_site.curriculum_skeleton_contract_errors(snapshot, pages)
        self.assertTrue(any("stray or malformed rows" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
