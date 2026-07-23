#!/usr/bin/env python3
"""スマートフォン／タブレットの実描画マトリクスを再現する。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from preview_server import PreviewHandler


ROOT = Path(__file__).resolve().parent
DEFAULT_CONTRACT = ROOT / "device_matrix.contract.json"
REPORT_PATH = ROOT / "review" / "browser" / "device-matrix-report.json"
PROFILE_KEYS = {
    "id",
    "label",
    "form_factor",
    "orientation",
    "width",
    "height",
    "text_scale",
}
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_PROFILE_SIGNATURES = {
    ("phone-compact", "phone", "portrait", 320, 568, 1.0),
    ("phone-android", "phone", "portrait", 360, 800, 1.0),
    ("phone-modern", "phone", "portrait", 390, 844, 1.0),
    ("phone-large", "phone", "portrait", 412, 915, 1.0),
    ("phone-landscape", "phone", "landscape", 844, 390, 1.0),
    ("tablet-small", "tablet", "portrait", 600, 960, 1.0),
    ("tablet-standard", "tablet", "portrait", 768, 1024, 1.0),
    ("tablet-large", "tablet", "portrait", 820, 1180, 1.0),
    ("tablet-landscape", "tablet", "landscape", 1024, 768, 1.0),
    ("phone-text-200", "phone", "portrait", 390, 844, 2.0),
}


class MatrixError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    label: str
    form_factor: str
    orientation: str
    width: int
    height: int
    text_scale: float


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def validate_profile(raw: object, index: int) -> DeviceProfile:
    if not isinstance(raw, dict):
        raise MatrixError(f"profiles[{index}]はobjectでなければなりません")
    keys = set(raw)
    if keys != PROFILE_KEYS:
        missing = sorted(PROFILE_KEYS - keys)
        unknown = sorted(keys - PROFILE_KEYS)
        raise MatrixError(
            f"profiles[{index}]のkey不一致: missing={missing}, unknown={unknown}"
        )

    profile_id = raw["id"]
    label = raw["label"]
    form_factor = raw["form_factor"]
    orientation = raw["orientation"]
    width = raw["width"]
    height = raw["height"]
    text_scale = raw["text_scale"]

    if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(
        profile_id
    ):
        raise MatrixError(f"profiles[{index}].idが安定ID形式ではありません")
    if not isinstance(label, str) or not label.strip():
        raise MatrixError(f"profiles[{index}].labelが空です")
    if form_factor not in {"phone", "tablet"}:
        raise MatrixError(f"profiles[{index}].form_factorが未知です")
    if orientation not in {"portrait", "landscape"}:
        raise MatrixError(f"profiles[{index}].orientationが未知です")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or not 280 <= width <= 2560
    ):
        raise MatrixError(f"profiles[{index}].widthが範囲外です")
    if (
        isinstance(height, bool)
        or not isinstance(height, int)
        or not 320 <= height <= 2560
    ):
        raise MatrixError(f"profiles[{index}].heightが範囲外です")
    if orientation == "portrait" and height < width:
        raise MatrixError(f"profiles[{index}]のportrait寸法が逆です")
    if orientation == "landscape" and width <= height:
        raise MatrixError(f"profiles[{index}]のlandscape寸法が逆です")
    if (
        isinstance(text_scale, bool)
        or not isinstance(text_scale, (int, float))
        or not 1.0 <= float(text_scale) <= 2.0
    ):
        raise MatrixError(f"profiles[{index}].text_scaleが範囲外です")

    return DeviceProfile(
        id=profile_id,
        label=label.strip(),
        form_factor=form_factor,
        orientation=orientation,
        width=width,
        height=height,
        text_scale=float(text_scale),
    )


def load_contract(path: Path) -> list[DeviceProfile]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatrixError(f"端末契約を読めません: {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "profiles",
    }:
        raise MatrixError("端末契約rootのkeyが不正です")
    if payload["schema_version"] != 1:
        raise MatrixError("端末契約schema_versionが未対応です")
    raw_profiles = payload["profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise MatrixError("端末契約profilesが空です")

    profiles = [
        validate_profile(raw, index) for index, raw in enumerate(raw_profiles)
    ]
    ids = [profile.id for profile in profiles]
    if len(ids) != len(set(ids)):
        raise MatrixError("端末契約に重複profile idがあります")

    signatures = {
        (
            profile.id,
            profile.form_factor,
            profile.orientation,
            profile.width,
            profile.height,
            profile.text_scale,
        )
        for profile in profiles
    }
    missing_signatures = REQUIRED_PROFILE_SIGNATURES - signatures
    if missing_signatures:
        raise MatrixError(
            "固定端末profileが欠落または改変されています: "
            + ", ".join(sorted(item[0] for item in missing_signatures))
        )

    required = {
        ("phone", "portrait"),
        ("phone", "landscape"),
        ("tablet", "portrait"),
        ("tablet", "landscape"),
    }
    observed = {
        (profile.form_factor, profile.orientation) for profile in profiles
    }
    if not required.issubset(observed):
        raise MatrixError(
            "phone/tabletのportrait/landscapeがすべて揃っていません"
        )
    if not any(profile.text_scale == 2.0 for profile in profiles):
        raise MatrixError("200%文字拡大profileがありません")
    if not any(
        profile.form_factor == "phone"
        and profile.orientation == "portrait"
        and profile.width <= 320
        for profile in profiles
    ):
        raise MatrixError("320px以下の小型スマートフォンprofileがありません")
    if not any(
        profile.form_factor == "phone"
        and profile.orientation == "portrait"
        and profile.width >= 412
        for profile in profiles
    ):
        raise MatrixError("412px以上の大型スマートフォンprofileがありません")
    return profiles


def validate_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.query
        or parsed.fragment
    ):
        raise MatrixError("--base-urlはlocalhostのHTTP(S)だけを指定できます")
    return base_url


def command_for_profile(
    profile: DeviceProfile,
    base_url: str,
    python: str = sys.executable,
) -> list[str]:
    return [
        python,
        "browser_check.py",
        "--viewport",
        f"{profile.width}x{profile.height}",
        "--profile-id",
        profile.id,
        "--text-scale",
        f"{profile.text_scale:g}",
        "--base-url",
        base_url,
    ]


class QuietPreviewHandler(PreviewHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class LocalPreview:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietPreviewHandler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="manabigrid-device-preview",
            daemon=True,
        )

    def __enter__(self) -> str:
        self.thread.start()
        port = int(self.server.server_address[1])
        return (
            f"http://127.0.0.1:{port}"
            f"{QuietPreviewHandler.base_path.rstrip('/')}"
        )

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def execute_profiles(
    profiles: list[DeviceProfile],
    base_url: str,
) -> tuple[list[dict[str, object]], int]:
    results: list[dict[str, object]] = []
    failures = 0
    for profile in profiles:
        command = command_for_profile(profile, base_url)
        browser_report = (
            ROOT
            / "review"
            / "browser"
            / f"browser-check-{profile.id}-report.json"
        )
        started_ns = time.time_ns()
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        report_is_current = (
            browser_report.is_file()
            and browser_report.stat().st_mtime_ns >= started_ns
        )
        if completed.returncode != 0 or not report_is_current:
            failures += 1
        results.append(
            {
                "profile": {
                    "id": profile.id,
                    "label": profile.label,
                    "form_factor": profile.form_factor,
                    "orientation": profile.orientation,
                    "width": profile.width,
                    "height": profile.height,
                    "text_scale": profile.text_scale,
                },
                "command": command,
                "returncode": completed.returncode,
                "browser_report": display_contract_path(browser_report),
                "browser_report_sha256": (
                    hashlib.sha256(browser_report.read_bytes()).hexdigest()
                    if report_is_current
                    else None
                ),
                "browser_report_current": report_is_current,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            }
        )
    return results, failures


def display_contract_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def evidence_hashes() -> dict[str, str]:
    paths = (
        ROOT / "device_matrix_check.py",
        ROOT / "browser_check.py",
        ROOT / "static" / "site.css",
        ROOT / "static" / "site.js",
        ROOT / "static" / "theme.js",
        ROOT / "build_site.py",
        ROOT / "device_matrix.contract.json",
    )
    return {
        display_contract_path(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT,
        help="端末マトリクス契約JSON",
    )
    parser.add_argument(
        "--base-url",
        help="すでに配信中のlocalhost site root（省略時は一時serverを起動）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="契約と実行commandだけを検証し、Chromeを起動しない",
    )
    args = parser.parse_args()

    try:
        contract_path = args.contract.resolve()
        profiles = load_contract(contract_path)
        base_url = (
            validate_base_url(args.base_url)
            if args.base_url
            else "http://127.0.0.1:0/manabigrid-site"
        )
    except MatrixError as exc:
        print(f"端末マトリクス: FAIL: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run_ready",
                    "profiles": len(profiles),
                    "commands": [
                        command_for_profile(profile, base_url)
                        for profile in profiles
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.base_url:
        results, failures = execute_profiles(profiles, base_url)
    else:
        with LocalPreview() as local_base_url:
            base_url = local_base_url
            results, failures = execute_profiles(profiles, base_url)

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "status": "ok" if failures == 0 else "failed",
        "contract": display_contract_path(contract_path),
        "contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "evidence_sha256": evidence_hashes(),
        "base_url": base_url,
        "profiles_total": len(profiles),
        "profiles_passed": len(profiles) - failures,
        "profiles_failed": failures,
        "results": results,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"端末マトリクス: {len(profiles) - failures}/{len(profiles)} profile、"
        f"失敗{failures}"
    )
    for result in results:
        print(f"- {result['profile']['id']}: {result['stdout']}")
        if result["stderr"]:
            print(result["stderr"], file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
