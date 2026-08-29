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

from preview_server import PreviewHandler, project_base_path
from public_site import iter_public_files


ROOT = Path(__file__).resolve().parent
DEFAULT_CONTRACT = ROOT / "device_matrix.contract.json"
REPORT_PATH = ROOT / "review" / "browser" / "device-matrix-report.json"
DEFAULT_SITE_ROOT = ROOT
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
    ("phone-compact-text-200", "phone", "portrait", 320, 568, 2.0),
    ("phone-text-200", "phone", "portrait", 390, 844, 2.0),
}
# 2026-08-29: mathml専用ページの引退（正本Issue #21裁定）で17→16
EXPECTED_RENDERED_PAGES = 16


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
    site_root: Path | None = None,
) -> list[str]:
    command = [
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
    if site_root is not None:
        command.extend(("--site-root", str(site_root.resolve())))
    return command


def validate_site_root(value: str | None = None) -> Path:
    if value is None:
        return DEFAULT_SITE_ROOT
    site_root = Path(value).resolve()
    if not site_root.is_dir():
        raise MatrixError("指定site rootは存在するディレクトリである必要があります")
    for name in ("index.html", "404.html", "build-report.json"):
        if not (site_root / name).is_file():
            raise MatrixError(f"指定site rootに{name}がありません: {site_root}")
    return site_root


class QuietPreviewHandler(PreviewHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class LocalPreview:
    def __init__(self, site_root: Path) -> None:
        self.site_root = site_root.resolve()
        self.previous_root = PreviewHandler.root
        self.previous_base_path = PreviewHandler.base_path
        PreviewHandler.root = self.site_root
        # Generated public candidates intentionally omit site.config.json.
        # The reviewed repository configuration defines the project base path;
        # only the files served below are switched to the fresh candidate.
        PreviewHandler.base_path = project_base_path()
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
        PreviewHandler.root = self.previous_root
        PreviewHandler.base_path = self.previous_base_path


def execute_profiles(
    profiles: list[DeviceProfile],
    base_url: str,
    site_root: Path,
) -> tuple[list[dict[str, object]], int]:
    results: list[dict[str, object]] = []
    failures = 0
    for profile in profiles:
        command = command_for_profile(profile, base_url, site_root=site_root)
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
        evidence_errors = (
            validate_browser_report_evidence(
                browser_report,
                profile,
                site_root,
                started_ns,
            )
            if report_is_current
            else ["browser reportが今回の実行で生成されていません"]
        )
        if (
            completed.returncode != 0
            or not report_is_current
            or evidence_errors
        ):
            failures += 1
        browser_payload: dict[str, object] = {}
        if report_is_current:
            try:
                loaded = json.loads(browser_report.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    browser_payload = loaded
            except (OSError, json.JSONDecodeError):
                pass
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
                "browser_report_evidence_errors": evidence_errors,
                "browser_version": browser_payload.get("browser_version"),
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


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise MatrixError("screenshotが有効なPNGではありません")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise MatrixError("screenshotのPNG寸法が不正です")
    return width, height


def validate_browser_report_evidence(
    report_path: Path,
    profile: DeviceProfile,
    site_root: Path,
    started_ns: int,
) -> list[str]:
    errors: list[str] = []
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"browser reportを読めません: {exc}"]
    if not isinstance(payload, dict):
        return ["browser report rootがobjectではありません"]
    expected_build_hash = hashlib.sha256(
        (site_root / "build-report.json").read_bytes()
    ).hexdigest()
    if payload.get("status") != "ok":
        errors.append("browser reportがokではありません")
    if payload.get("site_root") != str(site_root.resolve()):
        errors.append("browser reportのsite rootが候補と一致しません")
    if payload.get("build_report_sha256") != expected_build_hash:
        errors.append("browser reportのbuild-report hashが候補と一致しません")
    if payload.get("served_build_report_sha256") != expected_build_hash:
        errors.append("配信build-reportと候補rootのhashが一致しません")
    if payload.get("profile_id") != profile.id:
        errors.append("browser reportのprofile idが一致しません")
    if payload.get("text_scale") != profile.text_scale:
        errors.append("browser reportの文字倍率が一致しません")
    viewport = payload.get("viewport_css_pixels")
    if (
        not isinstance(viewport, dict)
        or viewport.get("width") != profile.width
        or viewport.get("height") != profile.height
    ):
        errors.append("browser reportのviewportがprofileと一致しません")
    version = payload.get("browser_version")
    if (
        not isinstance(version, dict)
        or not isinstance(version.get("product"), str)
        or not version["product"].strip()
    ):
        errors.append("Chrome/Chromium versionが記録されていません")
    pages = payload.get("pages")
    if not isinstance(pages, list) or len(pages) != EXPECTED_RENDERED_PAGES:
        errors.append(
            f"browser reportが{EXPECTED_RENDERED_PAGES}ページを記録していません"
        )
        return errors
    for page in pages:
        if not isinstance(page, dict):
            errors.append("browser reportのpage entryがobjectではありません")
            continue
        screenshot_value = page.get("screenshot")
        if not isinstance(screenshot_value, str):
            errors.append("screenshot pathがありません")
            continue
        screenshot = (ROOT / screenshot_value).resolve()
        try:
            screenshot.relative_to((ROOT / "review" / "browser").resolve())
        except ValueError:
            errors.append("screenshot pathがreview/browser外です")
            continue
        if (
            not screenshot.is_file()
            or screenshot.stat().st_mtime_ns < started_ns
        ):
            errors.append("screenshotが今回の実行で生成されていません")
            continue
        data = screenshot.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if page.get("screenshot_sha256") != digest:
            errors.append("screenshot hashが実ファイルと一致しません")
        try:
            width, height = _png_dimensions(data)
        except MatrixError as exc:
            errors.append(str(exc))
            continue
        pixels = page.get("screenshot_pixels")
        if (
            not isinstance(pixels, dict)
            or pixels.get("width") != width
            or pixels.get("height") != height
        ):
            errors.append("screenshot寸法の記録が実PNGと一致しません")
        if width != profile.width:
            errors.append("screenshot幅がprofileのCSS viewportと一致しません")
        if height != profile.height:
            errors.append("screenshot高さがprofileのCSS viewportと一致しません")
        metrics = page.get("metrics")
        if (
            not isinstance(metrics, dict)
            or metrics.get("innerWidth") != profile.width
            or metrics.get("innerHeight") != profile.height
        ):
            errors.append("実測CSS viewportがprofileと一致しません")
    return errors


def public_tree_fingerprint(site_root: Path) -> tuple[int, str]:
    files = iter_public_files(site_root)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(site_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        file_digest = hashlib.sha256(path.read_bytes()).digest()
        digest.update(file_digest)
    return len(files), digest.hexdigest()


def evidence_hashes() -> dict[str, str]:
    paths = (
        ROOT / "device_matrix_check.py",
        ROOT / "browser_check.py",
        ROOT / "static" / "site.css",
        ROOT / "static" / "site.js",
        ROOT / "static" / "theme.js",
        ROOT / "build_site.py",
        ROOT / "device_matrix.contract.json",
        ROOT / "preview_server.py",
        ROOT / "site.config.json",
        ROOT / "public_site.py",
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
        "--site-root",
        default=str(DEFAULT_SITE_ROOT),
        help="検査対象のsite root（既定は現行リポジトリroot）",
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
        site_root = validate_site_root(args.site_root)
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
                        command_for_profile(profile, base_url, site_root=site_root)
                        for profile in profiles
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.base_url:
        public_files_before, public_tree_before = public_tree_fingerprint(
            site_root
        )
        results, failures = execute_profiles(profiles, base_url, site_root)
    else:
        public_files_before, public_tree_before = public_tree_fingerprint(
            site_root
        )
        with LocalPreview(site_root) as local_base_url:
            base_url = local_base_url
            results, failures = execute_profiles(profiles, base_url, site_root)
    profile_failures = failures
    public_files_after, public_tree_after = public_tree_fingerprint(site_root)
    public_tree_unchanged = (
        public_files_before == public_files_after
        and public_tree_before == public_tree_after
    )
    integrity_failures = 0 if public_tree_unchanged else 1
    gate_failed = profile_failures > 0 or integrity_failures > 0

    report = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "status": "failed" if gate_failed else "ok",
        "contract": display_contract_path(contract_path),
        "contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "evidence_sha256": evidence_hashes(),
        "site_root": str(site_root),
        "build_report_sha256": hashlib.sha256((site_root / "build-report.json").read_bytes()).hexdigest(),
        "public_files": public_files_after,
        "public_tree_sha256_before": public_tree_before,
        "public_tree_sha256_after": public_tree_after,
        "public_tree_unchanged": public_tree_unchanged,
        "integrity_failures": integrity_failures,
        "base_url": base_url,
        "browser_versions": sorted(
            {
                str(version.get("product"))
                for result in results
                for version in [result.get("browser_version")]
                if isinstance(version, dict)
                and isinstance(version.get("product"), str)
            }
        ),
        "profiles_total": len(profiles),
        "profiles_passed": len(profiles) - profile_failures,
        "profiles_failed": profile_failures,
        "results": results,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"端末マトリクス: {len(profiles) - profile_failures}/{len(profiles)} profile、"
        f"profile失敗{profile_failures}、integrity失敗{integrity_failures}"
    )
    for result in results:
        print(f"- {result['profile']['id']}: {result['stdout']}")
        if result["stderr"]:
            print(result["stderr"], file=sys.stderr)
    return 1 if gate_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
