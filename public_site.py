"""GitHub Pagesへ載せる公開ファイルの単一allowlist。"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any


PUBLIC_ROOT_FILES = (
    ".nojekyll",
    "index.html",
    "404.html",
    "robots.txt",
    "sitemap.xml",
    "build-report.json",
)

PUBLIC_DIRECTORIES = (
    "_assets",
    "_media",
    "about",
    "browse",
    "content",
    "curriculum",
    "progress",
    "subjects",
    "units",
    "updates",
)

# Keep this equivalent to the source repository's quarantine workflow.  The
# artifact check intentionally examines only paths selected by this allowlist;
# it is not a claim about the source repository's history.
RAW_QUARANTINE_PATTERNS = (
    ("internal_only", re.compile(r"INTERNAL_ONLY")),
    ("local_users_path", re.compile(r"/Users/")),
    ("merge_log", re.compile(r"MERGE_LOG")),
    ("qa_report", re.compile(r"QA_REPORT")),
    ("submit_id", re.compile(r"submit[0-9]")),
    ("d_h_id", re.compile(r"D[0-9]+-H[0-9]+")),
    ("d_id", re.compile(r"\bD[0-9]{1,2}\b")),
    ("public_release_ready", re.compile(r"public_release_ready")),
    ("student_distribution_ready", re.compile(r"student_distribution_ready")),
    ("external_upload_ready", re.compile(r"external_upload_ready")),
    ("sale_ready", re.compile(r"sale_ready")),
    ("canonical_status", re.compile(r"canonical_status")),
    ("run_id", re.compile(r"run_id")),
    ("source_package", re.compile(r"source_package")),
    ("owner", re.compile("オーナー")),
    ("supervisor", re.compile("監督")),
    ("revision_queue", re.compile("改稿キュー")),
)


def _is_invisible_or_combining(character: str) -> bool:
    return (
        unicodedata.category(character) in {"Cf", "Mn", "Me", "Cn", "Co"}
        or 0xFE00 <= ord(character) <= 0xFE0F
        or 0x3099 <= ord(character) <= 0x309C
        or ord(character) in {0xFF9E, 0xFF9F}
    )


def _normalized_for_quarantine(value: str) -> str:
    stripped = "".join(ch for ch in value if not _is_invisible_or_combining(ch))
    normalized = unicodedata.normalize("NFKC", stripped)
    return "".join(ch for ch in normalized if not _is_invisible_or_combining(ch))


def _quarantine_labels(value: str) -> list[str]:
    """Return raw and Unicode-normalized quarantine hits without echoing text."""
    labels = [name for name, pattern in RAW_QUARANTINE_PATTERNS if pattern.search(value)]
    if any(_is_invisible_or_combining(ch) for ch in value):
        labels.append("invisible_or_combining_character")
    fullwidth_digit = "".join(chr(code) for code in range(0xFF10, 0xFF1A))
    fullwidth_upper = "".join(chr(code) for code in range(0xFF21, 0xFF3B))
    fake_id = re.compile(
        "(?:[A-Z][" + fullwidth_digit + "]|[" + fullwidth_upper + "][0-9" + fullwidth_digit + "])"
    )
    if fake_id.search(value):
        labels.append("fullwidth_obfuscated_id")

    normalized = _normalized_for_quarantine(value)
    dash = "-‐‑‒–—―−ー－" + "・･·./:_" + " \u00A0\t" + "•‣∙"
    separator = "[" + dash + "]+"
    normalized_patterns = (
        ("normalized_d_id", re.compile(r"(?<![0-9A-Za-z])D" + separator + r"[0-9]")),
        ("normalized_hr_id", re.compile(r"(?<![0-9A-Za-z])HR(?:" + separator + r")?[0-9]")),
        ("normalized_internal_label_en", re.compile("INTERNAL")),
        ("normalized_internal_label_ja", re.compile("内部用")),
        *RAW_QUARANTINE_PATTERNS,
    )
    labels.extend(
        "normalized_" + name
        for name, pattern in normalized_patterns
        if pattern.search(normalized)
    )
    return sorted(set(labels))


def iter_public_files(site_root: Path) -> list[Path]:
    """Return existing public files in stable relative-path order."""
    files: list[Path] = []
    for name in PUBLIC_ROOT_FILES:
        path = site_root / name
        if path.is_file():
            files.append(path)
    for name in PUBLIC_DIRECTORIES:
        directory = site_root / name
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    return sorted(files, key=lambda path: path.relative_to(site_root).as_posix())


def missing_public_entries(site_root: Path) -> list[str]:
    missing = [name for name in PUBLIC_ROOT_FILES if not (site_root / name).is_file()]
    missing.extend(name + "/" for name in PUBLIC_DIRECTORIES if not (site_root / name).is_dir())
    return missing


def quarantine_public_artifact(site_root: Path) -> list[dict[str, Any]]:
    """Inspect allowlisted artifact paths and UTF-8 bodies for quarantine hits.

    Binary/non-UTF-8 files have their path inspected but their body deliberately
    skipped.  This mirrors the source workflow's text-only Unicode scan while
    keeping the scope limited to the artifact selected by this module.
    """
    findings: list[dict[str, Any]] = []
    for path in iter_public_files(site_root):
        relative = path.relative_to(site_root).as_posix()
        path_labels = _quarantine_labels(relative)
        if path_labels:
            findings.append({"path": relative, "scope": "path", "issues": path_labels})
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        body_labels = _quarantine_labels(text)
        if body_labels:
            findings.append({"path": relative, "scope": "utf8_text", "issues": body_labels})
    return findings


def quarantine_self_test() -> bool:
    """Prove the raw and normalized checks catch representative evasions."""
    samples = (
        "INTERNAL_ONLY",
        "D" + "・" + "7",
        "ＭＥＲＧＥ＿ＬＯＧ",
        "run" + chr(0x200B) + "_id",
        "改稿" + "キ" + chr(0xFF9E) + "ュー",
    )
    return all(_quarantine_labels(sample) for sample in samples)
