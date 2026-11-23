from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_site


ENDPOINT = "https://script.google.com/macros/s/TEST/exec"


class RatingBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = build_site.Doc(
            Path("fixture/lesson_01.md"),
            Path("materials/jhs-math-1/jhs-math-1-example/lesson_01.md"),
            Path("content/materials/jhs-math-1/jhs-math-1-example/lesson_01.html"),
            "lesson", "例のレッスン", "jhs-math-1", "jhs-math-1-example",
            "0" * 64, False, 0,
        )
        self.unit = build_site.Unit(
            "jhs-math-1-example", "jhs-math-1", "例の単元", [self.doc]
        )
        self.enterContext(patch.dict(build_site.SITE_CONFIG, {"rating_endpoint": ""}))
        self.enterContext(patch.dict(build_site.BUILD_CONTEXT, {
            "commit": "unknown", "commit_date": "unknown", "generated_at": "unknown",
            "generated_date": "unknown", "base_url": build_site.DEFAULT_BASE_URL,
            "og_image_url": build_site.DEFAULT_BASE_URL + "_assets/og-manabigrid.png",
        }))

    def test_disabled_output_matches_pre_rating_golden_bytes(self) -> None:
        # SHA-256 values were measured from HEAD's unmodified generator before
        # implementing ratings, using these same fixed inputs and build context.
        with patch.object(Path, "read_text", return_value="# 例のレッスン\n本文\n"):
            actual = {
                "page": build_site.page(
                    Path("about/index.html"), "案内", "説明", "<p>本文</p>",
                    [("トップ", Path("index.html")), ("案内", None)], "page-about",
                ),
                "about": build_site.about_body(),
                "unit": build_site.unit_body(self.unit),
                "lesson": build_site.doc_body(
                    self.doc, "<p>本文</p>", {self.unit.slug: self.unit}
                ),
            }
        expected = {
            "page": "c10d7984312339d1e8de83c7b8949def52ae3314837576fccb98e17a225c86cf",
            "about": "9b952d92c0142e64fe9cd9c7b2613c690ddec3b2de695dbfc67ce0406f3da537",
            "unit": "85c5d55b976e7e43dac6e1f0286d907d8a2aa0547c469fb656829cfbd455a4c1",
            "lesson": "611fb0cef1f63faf51acab3bc2422a5a3cc1ff8fc0f69f5362d4bb8cbb4e2822",
        }
        for name, value in actual.items():
            with self.subTest(page=name):
                self.assertEqual(hashlib.sha256(value.encode()).hexdigest(), expected[name])
                self.assertNotIn("data-rating-", value)
                self.assertNotIn("_assets/rating.", value)

    def test_enabled_lesson_and_unit_have_hidden_hooks_and_scoped_assets(self) -> None:
        with patch.dict(build_site.SITE_CONFIG, {"rating_endpoint": ENDPOINT}):
            with patch.object(Path, "read_text", return_value="# 例のレッスン\n本文\n"):
                lesson = build_site.doc_body(self.doc, "<p>本文</p>", {self.unit.slug: self.unit})
            self.assertIn('data-rating-unit="jhs-math-1-example"', lesson)
            self.assertIn('data-rating-lesson="L01"', lesson)
            self.assertIn('data-rating-feedback-url="' + build_site.FORM_URL + '"', lesson)
            self.assertIn('hidden aria-label="このレッスンはどうでしたか"', lesson)
            self.assertEqual(lesson.count("data-rating-count-unit="), 1)
            unit = build_site.unit_body(self.unit)
            self.assertEqual(unit.count("data-rating-count-unit="), 1)
            self.assertNotIn("data-rating-lesson=", unit)
            page = build_site.page(self.doc.output, "例", "説明", lesson, [], "page-lesson")
            self.assertEqual(page.count("_assets/rating.js"), 1)
            self.assertEqual(page.count("_assets/rating.css"), 1)
            self.assertIn(
                "connect-src https://script.google.com https://script.googleusercontent.com;",
                page,
            )
            ordinary = build_site.page(Path("index.html"), "例", "説明", "<p>本文</p>", [], "page-home")
            self.assertNotIn("_assets/rating.", ordinary)
            self.assertIn("connect-src 'none';", ordinary)

    def test_enabled_about_explains_privacy_without_loading_rating_script(self) -> None:
        with patch.dict(build_site.SITE_CONFIG, {"rating_endpoint": ENDPOINT}):
            about = build_site.about_body()
        for phrase in ("評価ボタンで送るもの", "Google Apps Script", "単元ID", "レッスン番号",
                       "理由のコード", "Cookieは使いません", "押した日付だけ", "Googleの規約",
                       "ページを開くたび", "Googleの転送先（script.googleusercontent.com）"):
            self.assertIn(phrase, about)
        self.assertNotIn("data-rating-", about)

    def test_config_allows_only_exact_apps_script_deployment_url(self) -> None:
        config = json.loads(build_site.CONFIG_PATH.read_text())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "site.config.json"
            with patch.object(build_site, "CONFIG_PATH", path):
                for invalid in (None, 1, " " + ENDPOINT, ENDPOINT + "?unit=abc",
                                ENDPOINT + "#fragment", "https://evil.example/exec",
                                "https://script.google.com.evil.example/macros/s/TEST/exec",
                                "http://script.google.com/macros/s/TEST/exec",
                                "https://script.google.com:443/macros/s/TEST/exec"):
                    config["rating_endpoint"] = invalid
                    path.write_text(json.dumps(config))
                    with self.subTest(endpoint=invalid), self.assertRaises(RuntimeError):
                        build_site.load_site_config()
                for valid in ("", ENDPOINT):
                    config["rating_endpoint"] = valid
                    path.write_text(json.dumps(config))
                    self.assertEqual(build_site.load_site_config()["rating_endpoint"], valid)

    def test_optional_assets_are_emitted_only_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            image = source / build_site.OG_IMAGE_SOURCE
            image.parent.mkdir(parents=True)
            image.write_bytes(b"fixture")
            disabled = root / "disabled"
            build_site.copy_assets(source, disabled)
            self.assertFalse((disabled / "_assets/rating.js").exists())
            self.assertFalse((disabled / "_assets/rating.css").exists())
            enabled = root / "enabled"
            with patch.dict(build_site.SITE_CONFIG, {"rating_endpoint": ENDPOINT}):
                build_site.copy_assets(source, enabled)
            script = (enabled / "_assets/rating.js").read_text()
            self.assertIn('const ENDPOINT = "' + ENDPOINT + '";', script)
            self.assertNotIn("__RATING_ENDPOINT__", script)
            self.assertEqual((enabled / "_assets/site.css").read_bytes(),
                             (disabled / "_assets/site.css").read_bytes())


if __name__ == "__main__":
    unittest.main()
