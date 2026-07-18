#!/usr/bin/env python3
"""公開allowlistだけをGitHub Pages artifactへ複製する。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from public_site import iter_public_files, missing_public_entries, quarantine_public_artifact


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    site_root = args.site_root.resolve()
    missing = missing_public_entries(site_root)
    if missing:
        print("公開ファイル不足: " + ", ".join(missing))
        return 1

    quarantine_findings = quarantine_public_artifact(site_root)
    if quarantine_findings:
        print("公開artifact検疫に失敗しました: " + ", ".join(
            f"{item['path']} ({item['scope']}: {', '.join(item['issues'])})"
            for item in quarantine_findings
        ))
        return 1

    files = iter_public_files(site_root)
    total_bytes = sum(path.stat().st_size for path in files)
    if args.dry_run:
        print(f"Pages artifact dry-run: {len(files)}ファイル、{total_bytes} bytes、allowlist外0件")
        return 0

    if args.output is None:
        parser.error("--output または --dry-run が必要です")
    output = args.output.resolve()
    if output.exists():
        print(f"既存の出力先は上書きしません: {output}")
        return 1
    output.mkdir(parents=True)
    for source in files:
        relative = source.relative_to(site_root)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f"Pages artifact: {len(files)}ファイル、{total_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
