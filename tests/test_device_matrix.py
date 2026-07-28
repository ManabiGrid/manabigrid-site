from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.request import urlopen

import browser_check
import device_matrix_check


ROOT = Path(__file__).resolve().parents[1]


class DeviceMatrixContractTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return json.loads(
            (ROOT / "device_matrix.contract.json").read_text(encoding="utf-8")
        )

    def write_payload(self, payload: dict[str, object], root: Path) -> Path:
        path = root / "matrix.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_default_contract_covers_required_device_classes(self) -> None:
        profiles = device_matrix_check.load_contract(
            ROOT / "device_matrix.contract.json"
        )
        self.assertEqual(len(profiles), 11)
        self.assertIn(
            ("phone", "landscape"),
            {(profile.form_factor, profile.orientation) for profile in profiles},
        )
        self.assertIn(
            ("tablet", "landscape"),
            {(profile.form_factor, profile.orientation) for profile in profiles},
        )
        self.assertEqual(
            {(profile.width, profile.text_scale) for profile in profiles if profile.text_scale == 2.0},
            {(320, 2.0), (390, 2.0)},
        )
        self.assertEqual(browser_check.PRINT_VIEWPORT["width"], 794)
        self.assertFalse(browser_check.PRINT_VIEWPORT["mobile"])

    def test_browser_matrix_keeps_the_unit_resource_regression_page(self) -> None:
        pages = dict(browser_check.PAGES)
        self.assertEqual(len(browser_check.PAGES) + 1, 17)
        self.assertEqual(
            pages.get("unit-resources"),
            ROOT / "units/jhs-math-1-positive-negative-numbers/index.html",
        )
        self.assertEqual(
            pages.get("unit-resources-empty"),
            ROOT / "units/jhs-math-3-appendix/index.html",
        )
        self.assertEqual(
            set(browser_check.INLINE_MATH_BROWSER_EXPECTATIONS),
            {
                "math-inline-x-times",
                "math-inline-signed-fractions",
                "math-inline-variable-fraction",
            },
        )

    def test_duplicate_profile_id_is_rejected(self) -> None:
        payload = self.payload()
        profiles = payload["profiles"]
        assert isinstance(profiles, list)
        duplicate = copy.deepcopy(profiles[0])
        profiles.append(duplicate)
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_payload(payload, Path(temporary))
            with self.assertRaises(device_matrix_check.MatrixError):
                device_matrix_check.load_contract(path)

    def test_unknown_profile_key_is_rejected(self) -> None:
        payload = self.payload()
        profiles = payload["profiles"]
        assert isinstance(profiles, list)
        profile = profiles[0]
        assert isinstance(profile, dict)
        profile["guess"] = True
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_payload(payload, Path(temporary))
            with self.assertRaises(device_matrix_check.MatrixError):
                device_matrix_check.load_contract(path)

    def test_invalid_text_scale_is_rejected(self) -> None:
        payload = self.payload()
        profiles = payload["profiles"]
        assert isinstance(profiles, list)
        profile = profiles[-1]
        assert isinstance(profile, dict)
        profile["text_scale"] = 2.1
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_payload(payload, Path(temporary))
            with self.assertRaises(device_matrix_check.MatrixError):
                device_matrix_check.load_contract(path)

    def test_missing_tablet_landscape_is_rejected(self) -> None:
        payload = self.payload()
        profiles = payload["profiles"]
        assert isinstance(profiles, list)
        payload["profiles"] = [
            profile
            for profile in profiles
            if not (
                isinstance(profile, dict)
                and profile.get("form_factor") == "tablet"
                and profile.get("orientation") == "landscape"
            )
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_payload(payload, Path(temporary))
            with self.assertRaises(device_matrix_check.MatrixError):
                device_matrix_check.load_contract(path)

    def test_required_profile_dimensions_cannot_be_rewritten(self) -> None:
        payload = self.payload()
        profiles = payload["profiles"]
        assert isinstance(profiles, list)
        modern = next(
            profile
            for profile in profiles
            if isinstance(profile, dict) and profile.get("id") == "phone-modern"
        )
        modern["width"] = 375
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_payload(payload, Path(temporary))
            with self.assertRaisesRegex(
                device_matrix_check.MatrixError,
                "固定端末profile",
            ):
                device_matrix_check.load_contract(path)

    def test_profile_command_pins_viewport_scale_and_id(self) -> None:
        profile = device_matrix_check.DeviceProfile(
            id="phone-test",
            label="test",
            form_factor="phone",
            orientation="portrait",
            width=390,
            height=844,
            text_scale=2.0,
        )
        self.assertEqual(
            device_matrix_check.command_for_profile(
                profile,
                "http://127.0.0.1:8765/manabigrid-site",
                python="python3",
                site_root=ROOT,
            ),
            [
                "python3",
                "browser_check.py",
                "--viewport",
                "390x844",
                "--profile-id",
                "phone-test",
                "--text-scale",
                "2",
                "--base-url",
                "http://127.0.0.1:8765/manabigrid-site",
                "--site-root",
                str(ROOT),
            ],
        )

    def test_validate_site_root_requires_expected_build_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            (temporary_root / "index.html").write_text("ok", encoding="utf-8")
            (temporary_root / "404.html").write_text("ok", encoding="utf-8")
            with self.assertRaises(device_matrix_check.MatrixError):
                device_matrix_check.validate_site_root(str(temporary_root))

    def test_validate_site_root_accepts_valid_root(self) -> None:
        self.assertTrue(
            device_matrix_check.validate_site_root(str(ROOT)).is_absolute()
        )

    def test_local_preview_does_not_require_config_inside_generated_site(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            (candidate / "index.html").write_text(
                "fresh candidate marker",
                encoding="utf-8",
            )
            (candidate / "404.html").write_text("not found", encoding="utf-8")
            (candidate / "build-report.json").write_text(
                "{}",
                encoding="utf-8",
            )
            self.assertFalse((candidate / "site.config.json").exists())
            with device_matrix_check.LocalPreview(candidate) as base_url:
                self.assertTrue(
                    base_url.endswith("/manabigrid-site"),
                    base_url,
                )
                self.assertEqual(
                    browser_check.verify_base_url_site_root(
                        base_url,
                        candidate,
                    ),
                    browser_check.file_sha256(
                        candidate / "build-report.json"
                    ),
                )
                with urlopen(base_url + "/", timeout=2) as response:
                    self.assertEqual(
                        response.read().decode("utf-8"),
                        "fresh candidate marker",
                    )

    def test_local_preview_and_site_root_mismatch_is_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as served_directory,
            tempfile.TemporaryDirectory() as expected_directory,
        ):
            served = Path(served_directory)
            expected = Path(expected_directory)
            for root, report in (
                (served, '{"candidate":"served"}'),
                (expected, '{"candidate":"different"}'),
            ):
                (root / "index.html").write_text("ok", encoding="utf-8")
                (root / "404.html").write_text("not found", encoding="utf-8")
                (root / "build-report.json").write_text(
                    report,
                    encoding="utf-8",
                )
            with device_matrix_check.LocalPreview(served) as base_url:
                with self.assertRaisesRegex(RuntimeError, "一致しません"):
                    browser_check.verify_base_url_site_root(
                        base_url,
                        expected,
                    )

    def test_browser_viewport_and_scale_parsers_fail_closed(self) -> None:
        self.assertEqual(browser_check.viewport_argument("320x568"), (320, 568))
        self.assertEqual(browser_check.text_scale_argument("2"), 2.0)
        with self.assertRaises(Exception):
            browser_check.viewport_argument("320-by-568")
        with self.assertRaises(Exception):
            browser_check.text_scale_argument("2.01")

    def test_text_scale_must_reach_requested_computed_size(self) -> None:
        valid = {
            "requestedScale": 2.0,
            "rootBaselinePx": 16.0,
            "bodyBaselinePx": 16.0,
            "rootAppliedPx": 32.0,
            "bodyAppliedPx": 32.0,
        }
        self.assertEqual(
            browser_check.validate_text_scale_result(valid, 2.0),
            valid,
        )
        no_op = {**valid, "rootAppliedPx": 16.0, "bodyAppliedPx": 16.0}
        with self.assertRaisesRegex(RuntimeError, "要求どおり"):
            browser_check.validate_text_scale_result(no_op, 2.0)

    def test_matrix_evidence_binds_runner_browser_css_build_and_contract(self) -> None:
        hashes = device_matrix_check.evidence_hashes()
        self.assertEqual(
            set(hashes),
            {
                "device_matrix_check.py",
                "browser_check.py",
                "static/site.css",
                "static/site.js",
                "static/theme.js",
                "build_site.py",
                "device_matrix.contract.json",
                "preview_server.py",
                "site.config.json",
                "public_site.py",
            },
        )
        self.assertTrue(
            all(
                len(value) == 64
                and set(value).issubset(set("0123456789abcdef"))
                for value in hashes.values()
            )
        )

    def test_browser_report_evidence_binds_png_version_and_candidate(
        self,
    ) -> None:
        profile = device_matrix_check.DeviceProfile(
            id="evidence-test",
            label="evidence test",
            form_factor="phone",
            orientation="portrait",
            width=320,
            height=568,
            text_scale=1.0,
        )
        review_browser = ROOT / "review" / "browser"
        review_browser.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory() as site_directory,
            tempfile.TemporaryDirectory(
                dir=review_browser
            ) as report_directory,
        ):
            site_root = Path(site_directory)
            for name in ("index.html", "404.html"):
                (site_root / name).write_text(name, encoding="utf-8")
            (site_root / "build-report.json").write_text(
                '{"source":{"commit":"'
                + "a" * 40
                + '"}}',
                encoding="utf-8",
            )
            report_root = Path(report_directory)
            screenshot = report_root / "evidence.png"
            png = (
                b"\x89PNG\r\n\x1a\n"
                + b"\0" * 8
                + (320).to_bytes(4, "big")
                + (568).to_bytes(4, "big")
            )
            started_ns = time.time_ns()
            screenshot.write_bytes(png)
            screenshot_relative = screenshot.relative_to(ROOT).as_posix()
            screenshot_sha = hashlib.sha256(png).hexdigest()
            build_sha = browser_check.file_sha256(
                site_root / "build-report.json"
            )
            report = {
                "status": "ok",
                "site_root": str(site_root.resolve()),
                "build_report_sha256": build_sha,
                "served_build_report_sha256": build_sha,
                "profile_id": profile.id,
                "text_scale": profile.text_scale,
                "viewport_css_pixels": {
                    "width": profile.width,
                    "height": profile.height,
                },
                "browser_version": {"product": "HeadlessChrome/test"},
                "pages": [
                    {
                        "screenshot": screenshot_relative,
                        "screenshot_sha256": screenshot_sha,
                        "screenshot_pixels": {
                            "width": 320,
                            "height": 568,
                        },
                        "metrics": {
                            "innerWidth": 320,
                            "innerHeight": 568,
                        },
                    }
                    for _ in range(
                        device_matrix_check.EXPECTED_RENDERED_PAGES
                    )
                ],
            }
            report_path = report_root / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(
                device_matrix_check.validate_browser_report_evidence(
                    report_path,
                    profile,
                    site_root,
                    started_ns,
                ),
                [],
            )
            report["pages"][0]["metrics"]["innerHeight"] = 567
            report_path.write_text(json.dumps(report), encoding="utf-8")
            errors = device_matrix_check.validate_browser_report_evidence(
                report_path,
                profile,
                site_root,
                started_ns,
            )
            self.assertTrue(
                any("実測CSS viewport" in error for error in errors),
                errors,
            )
            report["pages"][0]["metrics"]["innerHeight"] = 568
            short_png = (
                b"\x89PNG\r\n\x1a\n"
                + b"\0" * 8
                + (320).to_bytes(4, "big")
                + (567).to_bytes(4, "big")
            )
            screenshot.write_bytes(short_png)
            short_sha = hashlib.sha256(short_png).hexdigest()
            for page in report["pages"]:
                page["screenshot_sha256"] = short_sha
                page["screenshot_pixels"]["height"] = 567
            report_path.write_text(json.dumps(report), encoding="utf-8")
            errors = device_matrix_check.validate_browser_report_evidence(
                report_path,
                profile,
                site_root,
                started_ns,
            )
            self.assertTrue(
                any("screenshot高さ" in error for error in errors),
                errors,
            )
            screenshot.write_bytes(png)
            for page in report["pages"]:
                page["screenshot_sha256"] = screenshot_sha
                page["screenshot_pixels"]["height"] = 568
            report["served_build_report_sha256"] = "b" * 64
            report_path.write_text(json.dumps(report), encoding="utf-8")
            errors = device_matrix_check.validate_browser_report_evidence(
                report_path,
                profile,
                site_root,
                started_ns,
            )
            self.assertTrue(
                any("配信build-report" in error for error in errors),
                errors,
            )


if __name__ == "__main__":
    unittest.main()
