from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

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
        self.assertEqual(len(profiles), 10)
        self.assertIn(
            ("phone", "landscape"),
            {(profile.form_factor, profile.orientation) for profile in profiles},
        )
        self.assertIn(
            ("tablet", "landscape"),
            {(profile.form_factor, profile.orientation) for profile in profiles},
        )
        self.assertTrue(any(profile.text_scale == 2.0 for profile in profiles))

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
            ],
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
                "build_site.py",
                "device_matrix.contract.json",
            },
        )
        self.assertTrue(
            all(
                len(value) == 64
                and set(value).issubset(set("0123456789abcdef"))
                for value in hashes.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
