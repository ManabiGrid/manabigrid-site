#!/usr/bin/env python3
"""Prove that the real-browser gate rejects a deliberate CSS page overflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from device_matrix_check import EXPECTED_RENDERED_PAGES, LocalPreview


ROOT = Path(__file__).resolve().parent
PROFILE_ID = "negative-css-overflow"
REPORT_PATH = (
    ROOT
    / "review"
    / "browser"
    / f"browser-check-{PROFILE_ID}-report.json"
)
EXPECTED_ERROR = "ページ全体が横にはみ出しています"


def validate_negative_report(report: object) -> list[str]:
    """Require every reviewed page to fail for the intended overflow reason."""

    errors: list[str] = []
    if not isinstance(report, dict) or report.get("status") != "failed":
        return ["browser reportがfailedではありません"]
    top_errors = report.get("errors")
    if (
        not isinstance(top_errors, list)
        or not any(EXPECTED_ERROR in str(error) for error in top_errors)
    ):
        errors.append("top-level errorsに期待したpage overflow理由がありません")
    pages = report.get("pages")
    if not isinstance(pages, list) or len(pages) != EXPECTED_RENDERED_PAGES:
        errors.append(
            f"browser reportが{EXPECTED_RENDERED_PAGES}ページを記録していません"
        )
        return errors
    labels: list[str] = []
    for page in pages:
        if not isinstance(page, dict):
            errors.append("browser reportのpage entryがobjectではありません")
            continue
        label = page.get("label")
        if not isinstance(label, str) or not label:
            errors.append("browser reportのpage labelがありません")
        else:
            labels.append(label)
        page_errors = page.get("errors")
        if (
            not isinstance(page_errors, list)
            or not any(EXPECTED_ERROR in str(error) for error in page_errors)
        ):
            errors.append(f"{label or '<unknown>'}: overflow errorがありません")
    if len(set(labels)) != len(labels):
        errors.append("browser reportのpage labelが重複しています")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site-root",
        type=Path,
        default=ROOT,
        help="正常候補のsite root（このroot自体は変更しない）",
    )
    args = parser.parse_args()
    site_root = args.site_root.resolve()
    if not site_root.is_dir():
        parser.error("--site-rootは既存ディレクトリで指定してください")
    review_root = ROOT / "review"
    review_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="manabigrid-overflow-negative-",
        dir=review_root,
    ) as temporary:
        candidate = Path(temporary) / "site"
        packaged = subprocess.run(
            [
                sys.executable,
                "package_site.py",
                "--site-root",
                str(site_root),
                "--output",
                str(candidate),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if packaged.returncode != 0:
            print(
                "CSS横あふれ負例: SETUP FAIL\n"
                + packaged.stdout
                + packaged.stderr,
                file=sys.stderr,
            )
            return 2

        css = candidate / "_assets" / "site.css"
        css.write_text(
            css.read_text(encoding="utf-8")
            + "\n/* Intentional adversarial fixture; temporary copy only. */\n"
            + "html, body { min-width: 1600px !important; }\n",
            encoding="utf-8",
        )
        started_ns = time.time_ns()
        with LocalPreview(candidate) as base_url:
            checked = subprocess.run(
                [
                    sys.executable,
                    "browser_check.py",
                    "--viewport",
                    "320x568",
                    "--profile-id",
                    PROFILE_ID,
                    "--base-url",
                    base_url,
                    "--site-root",
                    str(candidate),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

    if (
        checked.returncode == 0
        or not REPORT_PATH.is_file()
        or REPORT_PATH.stat().st_mtime_ns < started_ns
    ):
        print(
            "CSS横あふれ負例: FAIL（実ブラウザgateが停止しませんでした）\n"
            + checked.stdout
            + checked.stderr,
            file=sys.stderr,
        )
        return 1
    try:
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"CSS横あふれ負例: FAIL（report不正: {exc}）", file=sys.stderr)
        return 1
    validation_errors = validate_negative_report(report)
    if validation_errors:
        print(
            "CSS横あふれ負例: FAIL（期待したpage overflow理由では停止しませんでした）\n"
            + "\n".join(validation_errors),
            file=sys.stderr,
        )
        return 1
    errors = report["errors"]
    print(
        "CSS横あふれ負例: PASS "
        f"（320px実描画が{len(errors)}件のerrorで停止）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
