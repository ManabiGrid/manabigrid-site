from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import os
import posixpath
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlsplit

from public_site import (
    iter_public_files,
    missing_public_entries,
    quarantine_public_artifact,
    quarantine_self_test,
)


ALLOWED_EXTERNAL_HOSTS = {
    "creativecommons.org",
    "docs.google.com",
    "en.wikipedia.org",
    "github.com",
    "www.ndl.go.jp",
}
FORBIDDEN_STRINGS = [
    "fetch(",
    "xmlhttprequest",
    "sendbeacon",
    "analytics",
    "tracking",
    "sessionstorage",
]
BAD_SCHEMES = {"javascript", "data", "mailto", "tel"}
SOURCE_QUARANTINE_WORKFLOW_SHA256 = "b0dabf30bcfadcd1eff318e54361304feb64fb659d2e494a31b6651b8ca9bcb9"
OFFICIAL_SOURCE_REPOSITORY = "https://github.com/ManabiGrid/manabigrid"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ENGLISH_PASSAGE = re.compile(
    r"(?<![A-Za-z])(?:[A-Za-z][A-Za-z'’.-]*[,:;!?]?\s+){3,}"
    r"[A-Za-z][A-Za-z'’.-]*[,:;.!?]?"
)
UPDATE_FILE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".md", ".png", ".svg", ".webp"}
SAFE_SVG_TAGS = {
    "circle",
    "defs",
    "desc",
    "ellipse",
    "g",
    "line",
    "path",
    "pattern",
    "polygon",
    "polyline",
    "rect",
    "svg",
    "text",
    "title",
}
EXPECTED_CURRICULUM_STATUSES = (
    "未着手", "調査済", "ドラフト", "QA済", "外部レビュー済", "人間レビュー済", "公開済",
)
EXPECTED_CURRICULUM_FAMILY_ROWS = (
    ("jhs-math", "中学", "数学", "jhs-math-", "jhs-math"),
    ("jhs-eng", "中学", "英語", "jhs-eng-", "jhs-eng"),
    ("jhs-jpn", "中学", "国語", "jhs-jpn-", "jhs-jpn"),
    ("jhs-sci", "中学", "理科", "jhs-sci-", "jhs-sci"),
    ("jhs-soc", "中学", "社会", "jhs-soc-", "jhs-soc"),
    ("hs-math", "高校", "数学", "hs-math-", "hs-math"),
    ("hs-eng", "高校", "英語", "hs-eng-", "hs-eng"),
    ("hs-jpn", "高校", "国語", "hs-jpn-", "hs-jpn"),
    ("hs-sci", "高校", "理科", "hs-sci-", "hs-sci"),
    ("hs-soc", "高校", "社会", "hs-soc-", "hs-soc"),
)


@dataclass(frozen=True)
class HomeGridEntry:
    """One rendered top-page grid cell, collected independently from HTML."""

    subject: str
    packages: str
    lesson_pages: str
    hrefs: Tuple[str, ...]
    aria_labels: Tuple[str, ...]
    heading: str
    slots: int


@dataclass(frozen=True)
class CurriculumGridEntry:
    family: str
    unit_count: str
    package_count: str
    lesson_page_count: str
    availability: str
    school: str
    subject: str
    state_label: str
    visible_unit_count: str
    visible_availability: str
    hrefs: Tuple[str, ...]
    aria_labels: Tuple[str, ...]


class CurriculumGridCollector(HTMLParser):
    """Collect each ten-family card without trusting generator metadata."""

    VOID_TAGS = HomeGridCollector.VOID_TAGS if "HomeGridCollector" in globals() else {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: List[CurriculumGridEntry] = []
        self._depth = 0
        self._family = ""
        self._unit_count = ""
        self._package_count = ""
        self._lesson_page_count = ""
        self._availability = ""
        self._hrefs: List[str] = []
        self._aria_labels: List[str] = []
        self._capture: Optional[str] = None
        self._capture_depth: Optional[int] = None
        self._capture_parts: List[str] = []
        self._text: Dict[str, str] = {}

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        attrs_dict = {name: value or "" for name, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        if self._depth == 0:
            if tag != "li" or "curriculum-grid-item" not in classes:
                return
            self._depth = 1
            self._family = attrs_dict.get("data-family", "")
            self._unit_count = attrs_dict.get("data-unit-count", "")
            self._package_count = attrs_dict.get("data-package-count", "")
            self._lesson_page_count = attrs_dict.get("data-lesson-page-count", "")
            self._availability = attrs_dict.get("data-availability", "")
            self._hrefs = []
            self._aria_labels = []
            self._capture = None
            self._capture_depth = None
            self._capture_parts = []
            self._text = {}
            return
        if tag not in self.VOID_TAGS:
            self._depth += 1
        if tag == "a":
            self._hrefs.append(attrs_dict.get("href", ""))
            self._aria_labels.append(attrs_dict.get("aria-label", ""))
        field = None
        if "curriculum-school" in classes:
            field = "school"
        elif "curriculum-availability" in classes:
            field = "state"
        elif "curriculum-count" in classes:
            field = "visible_unit_count"
        elif "curriculum-availability-text" in classes:
            field = "visible_availability"
        elif tag == "h3":
            field = "subject"
        if field:
            self._capture = field
            self._capture_depth = self._depth
            self._capture_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0 or tag in self.VOID_TAGS:
            return
        if self._capture_depth is not None and self._depth == self._capture_depth:
            self._text[self._capture or ""] = " ".join(
                "".join(self._capture_parts).split()
            )
            self._capture = None
            self._capture_depth = None
            self._capture_parts = []
        self._depth -= 1
        if self._depth == 0:
            self.entries.append(
                CurriculumGridEntry(
                    family=self._family,
                    unit_count=self._unit_count,
                    package_count=self._package_count,
                    lesson_page_count=self._lesson_page_count,
                    availability=self._availability,
                    school=self._text.get("school", ""),
                    subject=self._text.get("subject", ""),
                    state_label=self._text.get("state", ""),
                    visible_unit_count=self._text.get("visible_unit_count", ""),
                    visible_availability=self._text.get("visible_availability", ""),
                    hrefs=tuple(self._hrefs),
                    aria_labels=tuple(self._aria_labels),
                )
            )


@dataclass(frozen=True)
class CurriculumSkeletonRow:
    item_id: str
    status: str
    material: str
    title: str
    visible_id: str
    visible_status: str
    state_kind: str
    state_href: str
    state_text: str


@dataclass(frozen=True)
class CurriculumSkeletonTrack:
    label: str
    count: int
    count_unit: str
    status_summary: str
    rows: Tuple[CurriculumSkeletonRow, ...]


CURRICULUM_TRACK_PATTERN = re.compile(
    r'<details class="curriculum-track">\s*'
    r'<summary><span><strong>(.*?)</strong><small>(\d+)(単元|件)</small></span>'
    r'<span class="track-status-summary">(.*?)</span></summary>\s*'
    r'<ol class="curriculum-unit-list">(.*?)</ol>\s*</details>',
    re.DOTALL,
)
CURRICULUM_ROW_PATTERN = re.compile(
    r'<li data-curriculum-id="([^"]+)" data-status="([^"]+)" '
    r'data-material="(available|preparing)">\s*'
    r'<div><strong>(.*?)</strong>'
    r'(?:<span class="curriculum-unit-grade">.*?</span>)?'
    r'<code>(.*?)</code></div>\s*'
    r'<div class="curriculum-unit-state"><span class="status-chip">(.*?)</span>'
    r'(?:<a class="curriculum-unit-link" href="([^"]+)">(.*?)</a>'
    r'|<span class="curriculum-unit-pending">(.*?)</span>)</div>\s*</li>',
    re.DOTALL,
)


def _curriculum_visible_text(value: str) -> str:
    return " ".join(
        html_module.unescape(re.sub(r"<[^>]*>", " ", value)).split()
    )


def collect_curriculum_skeleton_tracks(raw: str) -> Tuple[CurriculumSkeletonTrack, ...]:
    tracks: List[CurriculumSkeletonTrack] = []
    for label, count, count_unit, status_summary, rows_raw in CURRICULUM_TRACK_PATTERN.findall(raw):
        rows = tuple(
            CurriculumSkeletonRow(
                item_id=html_module.unescape(item_id),
                status=html_module.unescape(status),
                material=material,
                title=_curriculum_visible_text(title),
                visible_id=_curriculum_visible_text(visible_id),
                visible_status=_curriculum_visible_text(visible_status),
                state_kind="link" if state_href else "pending",
                state_href=html_module.unescape(state_href),
                state_text=_curriculum_visible_text(state_link_text or state_pending_text),
            )
            for (
                item_id,
                status,
                material,
                title,
                visible_id,
                visible_status,
                state_href,
                state_link_text,
                state_pending_text,
            ) in CURRICULUM_ROW_PATTERN.findall(rows_raw)
        )
        tracks.append(
            CurriculumSkeletonTrack(
                label=_curriculum_visible_text(label),
                count=int(count),
                count_unit=count_unit,
                status_summary=_curriculum_visible_text(status_summary),
                rows=rows,
            )
        )
    return tuple(tracks)


def curriculum_status_summary_from_items(items: Sequence[Dict[str, str]]) -> str:
    counts = Counter(str(item["status"]) for item in items)
    return "、".join(
        f"{status} {counts[status]}"
        for status in EXPECTED_CURRICULUM_STATUSES
        if counts.get(status)
    )


class HomeGridCollector(HTMLParser):
    """Collect grid-cell contracts without relying on generator metadata."""

    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: List[HomeGridEntry] = []
        self._depth = 0
        self._subject = ""
        self._packages = ""
        self._lesson_pages = ""
        self._hrefs: List[str] = []
        self._aria_labels: List[str] = []
        self._heading_parts: List[str] = []
        self._heading_depth: Optional[int] = None
        self._slots = 0

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        attrs_dict = {name: value or "" for name, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        if self._depth == 0:
            if tag != "li" or "learning-grid-item" not in classes:
                return
            self._depth = 1
            self._subject = attrs_dict.get("data-subject", "")
            self._packages = attrs_dict.get("data-package-count", "")
            self._lesson_pages = attrs_dict.get("data-lesson-page-count", "")
            self._hrefs = []
            self._aria_labels = []
            self._heading_parts = []
            self._heading_depth = None
            self._slots = 0
            return

        if tag not in self.VOID_TAGS:
            self._depth += 1
        if tag == "a":
            self._hrefs.append(attrs_dict.get("href", ""))
            self._aria_labels.append(attrs_dict.get("aria-label", ""))
        if "learning-grid-heading" in classes:
            self._heading_depth = self._depth
        if "learning-grid-slot" in classes:
            self._slots += 1

    def handle_data(self, data: str) -> None:
        if self._heading_depth is not None and self._depth >= self._heading_depth:
            self._heading_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0 or tag in self.VOID_TAGS:
            return
        if self._heading_depth is not None and self._depth == self._heading_depth:
            self._heading_depth = None
        self._depth -= 1
        if self._depth == 0:
            self.entries.append(
                HomeGridEntry(
                    subject=self._subject,
                    packages=self._packages,
                    lesson_pages=self._lesson_pages,
                    hrefs=tuple(self._hrefs),
                    aria_labels=tuple(self._aria_labels),
                    heading=" ".join("".join(self._heading_parts).split()),
                    slots=self._slots,
                )
            )


def canonical_home_grid_counts(source: Path) -> Dict[str, Tuple[int, int]]:
    """Count packages and lesson pages directly from canonical materials."""
    materials = source / "materials"
    if not materials.is_dir():
        raise FileNotFoundError(f"canonical materials directory not found: {materials}")

    result: Dict[str, Tuple[int, int]] = {}
    for subject_dir in sorted(
        path
        for path in materials.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ):
        package_dirs = [
            path
            for path in sorted(subject_dir.iterdir())
            if path.is_dir()
            and not path.name.startswith(".")
            and any(path.rglob("*.md"))
        ]
        lesson_pages = sum(
            1
            for package_dir in package_dirs
            for markdown in package_dir.rglob("*.md")
            if re.fullmatch(r"lesson_\d+\.md", markdown.name)
            or markdown.name in {"diagnostic.md", "student_textbook_print.md"}
        )
        result[subject_dir.name] = (len(package_dirs), lesson_pages)
    return result


def home_grid_contract_errors(
    canonical: Dict[str, Tuple[int, int]], report: object, html_text: str
) -> List[str]:
    """Compare report and rendered grid with a source-derived canonical map."""
    errors: List[str] = []
    if not isinstance(report, dict):
        return ["build-report missing home_grid"]
    reported_subjects = report.get("subjects")
    if (
        report.get("source") != "subject_registry_and_collected_units"
        or not isinstance(reported_subjects, list)
        or not reported_subjects
    ):
        return ["build-report home_grid contract mismatch"]

    report_rows: List[Tuple[str, str, int, int]] = []
    for item in reported_subjects:
        if not isinstance(item, dict):
            errors.append("home_grid subject row is not an object")
            continue
        slug = item.get("slug")
        label = item.get("label")
        packages = item.get("packages")
        lesson_pages = item.get("lesson_pages")
        if (
            not isinstance(slug, str)
            or not slug
            or not isinstance(label, str)
            or not label
            or type(packages) is not int
            or packages < 0
            or type(lesson_pages) is not int
            or lesson_pages < 0
        ):
            errors.append("home_grid subject row has invalid fields")
            continue
        report_rows.append((slug, label, packages, lesson_pages))

    report_slugs = [slug for slug, _label, _packages, _lessons in report_rows]
    report_labels = [label for _slug, label, _packages, _lessons in report_rows]
    if len(report_slugs) != len(set(report_slugs)):
        errors.append("home_grid report contains duplicate subjects")
    if len(report_labels) != len(set(report_labels)):
        errors.append("home_grid report contains duplicate labels")
    missing_report = sorted(set(canonical) - set(report_slugs))
    extra_report = sorted(set(report_slugs) - set(canonical))
    if missing_report:
        errors.append("home_grid report omits canonical subjects: " + ", ".join(missing_report))
    if extra_report:
        errors.append("home_grid report has non-canonical subjects: " + ", ".join(extra_report))
    for slug, _label, packages, lesson_pages in report_rows:
        expected = canonical.get(slug)
        if expected is not None and expected != (packages, lesson_pages):
            errors.append(f"home_grid report count differs from canonical source: {slug}")

    canonical_packages = sum(packages for packages, _lessons in canonical.values())
    canonical_lessons = sum(lessons for _packages, lessons in canonical.values())
    if report.get("total_packages") != canonical_packages:
        errors.append("home_grid total_packages differs from canonical source")
    if report.get("total_lesson_pages") != canonical_lessons:
        errors.append("home_grid total_lesson_pages differs from canonical source")

    collector = HomeGridCollector()
    collector.feed(html_text)
    entries = collector.entries
    rendered_slugs = [entry.subject for entry in entries]
    rendered_headings = [entry.heading for entry in entries]
    if len(rendered_slugs) != len(set(rendered_slugs)):
        errors.append("home learning grid contains duplicate subjects")
    if len(rendered_headings) != len(set(rendered_headings)):
        errors.append("home learning grid contains duplicate visible labels")
    missing_rendered = sorted(set(canonical) - set(rendered_slugs))
    extra_rendered = sorted(set(rendered_slugs) - set(canonical))
    if missing_rendered:
        errors.append("home learning grid omits canonical subjects: " + ", ".join(missing_rendered))
    if extra_rendered:
        errors.append("home learning grid has non-canonical subjects: " + ", ".join(extra_rendered))
    if rendered_slugs != report_slugs:
        errors.append("home learning grid order or membership differs from home_grid report")

    report_label_by_slug = {
        slug: label for slug, label, _packages, _lessons in report_rows
    }

    for entry in entries:
        expected = canonical.get(entry.subject)
        if expected is None:
            continue
        expected_label = report_label_by_slug.get(entry.subject)
        if entry.heading != expected_label:
            errors.append(f"home learning grid visible label mismatch: {entry.subject}")
        try:
            rendered_counts = (int(entry.packages), int(entry.lesson_pages))
        except ValueError:
            errors.append(f"home learning grid has invalid count attributes: {entry.subject}")
            continue
        if rendered_counts != expected:
            errors.append(f"home learning grid count differs from canonical source: {entry.subject}")
        expected_href = f"subjects/{entry.subject}/index.html"
        if entry.hrefs != (expected_href,):
            errors.append(f"home learning grid subject href mismatch: {entry.subject}")
        expected_aria = (
            f"{expected_label}を開く。{expected[0]}パッケージ、"
            f"レッスン・本文{expected[1]}ページ"
        )
        if entry.aria_labels != (expected_aria,):
            errors.append(f"home learning grid aria-label mismatch: {entry.subject}")
        if entry.slots != expected[0]:
            errors.append(f"home learning grid slot count differs from canonical source: {entry.subject}")

    if sum(entry.slots for entry in entries) != canonical_packages:
        errors.append("home learning grid total slot count differs from canonical source")
    return errors


def _curriculum_text(value: str) -> str:
    return re.sub(r"[`*_~]", "", value).strip()


def _curriculum_cells(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _curriculum_separator(cells: Sequence[str], width: int) -> bool:
    return len(cells) == width and all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in cells
    )


def canonical_curriculum_snapshot(source: Path, contract_path: Path) -> Dict[str, object]:
    """Independently derive the ten entrances from contract + source + materials."""
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"curriculum contract cannot be read: {exc}") from exc
    version = contract.get("schema_version")
    statuses = contract.get("status_values")
    family_rows = contract.get("families")
    if (
        version != 1
        or contract.get("source") != "curriculum/PROGRESS_INDEX.md"
        or not isinstance(statuses, list)
        or not statuses
        or len(statuses) != len(set(statuses))
        or not all(isinstance(value, str) and value for value in statuses)
        or not isinstance(family_rows, list)
        or len(family_rows) != 10
    ):
        raise ValueError("curriculum contract schema mismatch")
    families: List[Dict[str, object]] = []
    for row in family_rows:
        if not isinstance(row, dict):
            raise ValueError("curriculum family row is not an object")
        values = {
            key: row.get(key)
            for key in ("slug", "school", "subject", "unit_id_prefix", "material_prefix")
        }
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ValueError("curriculum family fields are invalid")
        families.append(values)
    actual_rows = tuple(
        (
            str(row["slug"]),
            str(row["school"]),
            str(row["subject"]),
            str(row["unit_id_prefix"]),
            str(row["material_prefix"]),
        )
        for row in families
    )
    if tuple(statuses) != EXPECTED_CURRICULUM_STATUSES:
        raise ValueError("curriculum contract statuses differ from fixed expectations")
    if actual_rows != EXPECTED_CURRICULUM_FAMILY_ROWS:
        raise ValueError("curriculum contract families differ from fixed expectations")

    progress = source / "curriculum/PROGRESS_INDEX.md"
    raw = progress.read_text(encoding="utf-8")
    section = ""
    unit_header = False
    module_header = False
    unit_separator = False
    module_separator = False
    units: List[Dict[str, str]] = []
    modules: List[Dict[str, str]] = []
    for line in raw.splitlines():
        if line.startswith("## "):
            if line == "## 全単元一覧（unit_id 順）":
                section = "units"
            elif line == "## 科目モジュール（単元と別枠: 診断・巻末資料）":
                section = "modules"
            else:
                section = ""
            continue
        if section not in {"units", "modules"}:
            continue
        if not line:
            continue
        if not line.startswith("|"):
            raise ValueError(f"curriculum {section} table has non-table or indented row")
        cells = _curriculum_cells(line)
        if section == "units":
            if cells == ["unit_id", "単元名", "科目", "学校段階・学年", "レーン", "状態"]:
                if unit_header:
                    raise ValueError("curriculum unit header is duplicated")
                unit_header = True
                continue
            if _curriculum_separator(cells, 6):
                if not unit_header or unit_separator:
                    raise ValueError("curriculum unit header missing before separator")
                unit_separator = True
                continue
            if not unit_header or not unit_separator:
                raise ValueError("curriculum unit header missing before rows")
            if len(cells) != 6 or not re.fullmatch(r"`[^`]+`", cells[0]):
                raise ValueError("curriculum unit table has malformed row")
            item_id, title, subject, grade, lane, status = cells
            units.append(
                {
                    "id": item_id.strip("`"),
                    "title": _curriculum_text(title),
                    "subject": _curriculum_text(subject),
                    "grade": _curriculum_text(grade),
                    "lane": _curriculum_text(lane),
                    "status": _curriculum_text(status),
                    "kind": "unit",
                }
            )
        elif section == "modules":
            if cells == ["module_id", "名称", "科目", "学校段階・学年", "状態"]:
                if module_header:
                    raise ValueError("curriculum module header is duplicated")
                module_header = True
                continue
            if _curriculum_separator(cells, 5):
                if not module_header or module_separator:
                    raise ValueError("curriculum module header missing before separator")
                module_separator = True
                continue
            if not module_header or not module_separator:
                raise ValueError("curriculum module header missing before rows")
            if len(cells) != 5 or not re.fullmatch(r"`[^`]+`", cells[0]):
                raise ValueError("curriculum module table has malformed row")
            item_id, title, subject, grade, status = cells
            modules.append(
                {
                    "id": item_id.strip("`"),
                    "title": _curriculum_text(title),
                    "subject": _curriculum_text(subject),
                    "grade": _curriculum_text(grade),
                    "lane": "",
                    "status": _curriculum_text(status),
                    "kind": "module",
                }
            )
    if (
        not unit_header or not unit_separator or not units
        or not module_header or not module_separator or not modules
    ):
        raise ValueError("curriculum canonical tables are missing")
    items = units + modules
    ids = [item["id"] for item in items]
    duplicates = sorted(item_id for item_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError("curriculum duplicate ids: " + ", ".join(duplicates[:8]))
    allowed_subjects = {"数学", "英語", "国語", "理科", "社会"}
    for item in items:
        if not all(item[key] for key in ("id", "title", "subject", "grade", "status")):
            raise ValueError(f"curriculum empty field: {item['id'] or '(missing id)'}")
        if item["subject"] not in allowed_subjects:
            raise ValueError(f"curriculum unknown subject: {item['subject']}")
        if item["status"] not in statuses:
            raise ValueError(f"curriculum unknown status: {item['status']}")
        if item["kind"] == "unit" and item["lane"] != "公開コア":
            raise ValueError(f"curriculum unknown lane: {item['id']}")

    family_items: Dict[str, List[Dict[str, str]]] = {
        str(row["slug"]): [] for row in families
    }
    for item in units:
        matches = [
            row for row in families if item["id"].startswith(str(row["unit_id_prefix"]))
        ]
        if len(matches) != 1:
            raise ValueError(f"curriculum unit family match must be unique: {item['id']}")
        row = matches[0]
        if item["subject"] != row["subject"]:
            raise ValueError(f"curriculum unit subject mismatch: {item['id']}")
        family_items[str(row["slug"])].append(item)
    empty_families = [slug for slug, rows in family_items.items() if not rows]
    if empty_families:
        raise ValueError("curriculum empty families: " + ", ".join(empty_families))

    materials = source / "materials"
    if not materials.is_dir():
        raise ValueError("canonical materials directory missing")
    material_packages: Dict[str, List[str]] = {}
    lesson_counts: Dict[str, int] = {}
    for subject_dir in sorted(path for path in materials.iterdir() if path.is_dir()):
        packages = [
            path.name
            for path in sorted(subject_dir.iterdir())
            if path.is_dir() and any(path.rglob("*.md"))
        ]
        material_packages[subject_dir.name] = packages
        lesson_counts[subject_dir.name] = sum(
            1
            for package in packages
            for markdown in (subject_dir / package).rglob("*.md")
            if re.fullmatch(r"lesson_\d+\.md", markdown.name)
            or markdown.name in {"diagnostic.md", "student_textbook_print.md"}
        )
    subject_family: Dict[str, str] = {}
    for subject in material_packages:
        matches = [
            row
            for row in families
            if subject == row["material_prefix"]
            or subject.startswith(str(row["material_prefix"]) + "-")
        ]
        if len(matches) != 1:
            raise ValueError(f"material subject family match must be unique: {subject}")
        subject_family[subject] = str(matches[0]["slug"])

    package_to_items: Dict[str, List[Dict[str, str]]] = {}
    item_material: Dict[str, str] = {}
    all_package_slugs = [slug for rows in material_packages.values() for slug in rows]
    for item in items:
        candidates = [
            slug
            for slug in all_package_slugs
            if item["id"] == slug or item["id"].startswith(slug + "--")
        ]
        if candidates:
            longest = max(map(len, candidates))
            winners = [slug for slug in candidates if len(slug) == longest]
            if len(winners) != 1:
                raise ValueError(f"curriculum item package match is ambiguous: {item['id']}")
            item_material[item["id"]] = winners[0]
            package_to_items.setdefault(winners[0], []).append(item)
    for package in all_package_slugs:
        linked = package_to_items.get(package, [])
        if not linked:
            raise ValueError(f"material package has no curriculum item: {package}")
        if all(item["status"] in {"未着手", "調査済"} for item in linked):
            raise ValueError(f"material package has only pre-body statuses: {package}")

    expected_families: List[Dict[str, object]] = []
    for row in families:
        slug = str(row["slug"])
        subjects = [
            subject for subject, family_slug in subject_family.items() if family_slug == slug
        ]
        package_count = sum(len(material_packages[subject]) for subject in subjects)
        expected_families.append(
            {
                "slug": slug,
                "unit_id_prefix": row["unit_id_prefix"],
                "school": row["school"],
                "subject": row["subject"],
                "label": f"{row['school']} {row['subject']}",
                "href": f"curriculum/{slug}/index.html",
                "registered_units": len(family_items[slug]),
                "status_counts": dict(Counter(item["status"] for item in family_items[slug])),
                "material_subjects": subjects,
                "packages": package_count,
                "lesson_pages": sum(lesson_counts[subject] for subject in subjects),
                "availability": "available" if package_count else "preparing",
            }
        )
    return {
        "contract_version": version,
        "progress_index_sha256": hashlib.sha256(progress.read_bytes()).hexdigest(),
        "statuses": statuses,
        "families": expected_families,
        "units": units,
        "modules": modules,
        "item_material": item_material,
        "total_packages": len(all_package_slugs),
        "total_lesson_pages": sum(lesson_counts.values()),
    }


def _curriculum_relative_href(current: PurePosixPath, target: str) -> str:
    return posixpath.relpath(target, current.parent.as_posix() or ".")


def curriculum_grid_contract_errors(
    snapshot: Dict[str, object], report: object, html_text: str, current: PurePosixPath
) -> List[str]:
    """Compare one rendered grid and report with the independently derived map."""
    errors: List[str] = []
    families = snapshot.get("families")
    if not isinstance(families, list):
        return ["canonical curriculum families are invalid"]
    if not isinstance(report, dict):
        return ["build-report missing curriculum_grid"]
    reported = report.get("families")
    if (
        report.get("source") != "curriculum_progress_index_and_canonical_materials"
        or report.get("contract_version") != snapshot.get("contract_version")
        or report.get("progress_index_sha256") != snapshot.get("progress_index_sha256")
        or not isinstance(reported, list)
    ):
        errors.append("build-report curriculum_grid contract mismatch")
        reported = []
    expected_slugs = [str(row["slug"]) for row in families]
    reported_slugs = [str(row.get("slug", "")) for row in reported if isinstance(row, dict)]
    if reported_slugs != expected_slugs:
        errors.append("curriculum_grid report order or membership differs from canonical source")
    for expected, actual in zip(families, reported):
        if not isinstance(actual, dict):
            errors.append("curriculum_grid report family row is invalid")
            continue
        for key in (
            "slug", "label", "school", "subject", "href", "registered_units",
            "status_counts", "material_subjects", "packages", "lesson_pages", "availability",
        ):
            if actual.get(key) != expected.get(key):
                errors.append(f"curriculum_grid report {key} differs: {expected['slug']}")
    available = sum(row["availability"] == "available" for row in families)
    preparing = len(families) - available
    totals = {
        "total_entries": len(families),
        "available_entries": available,
        "preparing_entries": preparing,
        "registered_units": len(snapshot.get("units", [])),
        "registered_modules": len(snapshot.get("modules", [])),
        "total_packages": snapshot.get("total_packages"),
        "total_lesson_pages": snapshot.get("total_lesson_pages"),
    }
    for key, expected in totals.items():
        if report.get(key) != expected:
            errors.append(f"curriculum_grid report {key} differs from canonical source")

    if current == PurePosixPath("index.html"):
        expected_hero_fragments = (
            f"OPEN LEARNING / {len(families)} CURRICULUM ENTRANCES",
            (
                "わからなくなった場所まで戻り、自分のペースで学び直せます。"
                f"中学・高校{len(families)}入口のうち、{available}入口で"
                f"{snapshot.get('total_packages')}パッケージを読めます。"
                f"{preparing}入口は「準備中」です。登録も記録もいりません。"
            ),
        )
        if any(fragment not in html_text for fragment in expected_hero_fragments):
            errors.append("home curriculum hero totals or wording differ from canonical source")

    collector = CurriculumGridCollector()
    collector.feed(html_text)
    entries = collector.entries
    rendered_slugs = [entry.family for entry in entries]
    if rendered_slugs != expected_slugs:
        errors.append("curriculum grid order or membership differs from canonical source")
    by_slug = {str(row["slug"]): row for row in families}
    for entry in entries:
        expected = by_slug.get(entry.family)
        if expected is None:
            errors.append(f"curriculum grid has non-canonical family: {entry.family}")
            continue
        try:
            counts = (
                int(entry.unit_count), int(entry.package_count), int(entry.lesson_page_count)
            )
        except ValueError:
            errors.append(f"curriculum grid has invalid counts: {entry.family}")
            continue
        if counts != (
            expected["registered_units"], expected["packages"], expected["lesson_pages"]
        ):
            errors.append(f"curriculum grid counts differ: {entry.family}")
        if entry.availability != expected["availability"]:
            errors.append(f"curriculum grid availability differs: {entry.family}")
        if entry.school != expected["school"] or entry.subject != expected["subject"]:
            errors.append(f"curriculum grid visible labels differ: {entry.family}")
        state_label = "教材あり" if expected["availability"] == "available" else "準備中"
        if entry.state_label != state_label:
            errors.append(f"curriculum grid visible state differs: {entry.family}")
        expected_unit_text = f"進捗表に{expected['registered_units']}単元"
        if entry.visible_unit_count != expected_unit_text:
            errors.append(f"curriculum grid visible unit count differs: {entry.family}")
        expected_availability_text = (
            f"{expected['packages']}パッケージ・レッスン／本文{expected['lesson_pages']}ページを掲載"
            if expected["availability"] == "available"
            else "このサイトで読める教材は、まだありません"
        )
        if entry.visible_availability != expected_availability_text:
            errors.append(f"curriculum grid visible availability differs: {entry.family}")
        target = _curriculum_relative_href(current, str(expected["href"]))
        material_hrefs = [
            _curriculum_relative_href(current, f"subjects/{subject}/index.html")
            for subject in expected["material_subjects"]
        ]
        if list(entry.hrefs) != [target, *material_hrefs, target]:
            errors.append(f"curriculum grid hrefs differ: {entry.family}")
        expected_aria = (
            f"{expected['label']}の学習グリッドを開く。進捗表に{expected['registered_units']}単元、"
            + (
                f"{expected['packages']}パッケージ掲載"
                if expected["availability"] == "available"
                else "教材は準備中"
            )
        )
        if not entry.aria_labels or entry.aria_labels[0] != expected_aria:
            errors.append(f"curriculum grid aria-label differs: {entry.family}")
        expected_open_aria = f"{expected['label']}の単元の全体像を見る"
        if len(entry.aria_labels) < 2 or entry.aria_labels[-1] != expected_open_aria:
            errors.append(f"curriculum grid open-link aria-label differs: {entry.family}")
    return errors


def curriculum_skeleton_contract_errors(
    snapshot: Dict[str, object], html_by_path: Dict[str, str]
) -> List[str]:
    """Validate all family skeleton rows and the separate module skeleton."""
    errors: List[str] = []
    families = snapshot.get("families")
    units = snapshot.get("units")
    modules = snapshot.get("modules")
    item_material = snapshot.get("item_material")
    if (
        not isinstance(families, list)
        or not isinstance(units, list)
        or not isinstance(modules, list)
        or not isinstance(item_material, dict)
    ):
        return ["canonical curriculum skeleton snapshot is invalid"]
    def expected_row(item: Dict[str, str], current: PurePosixPath) -> CurriculumSkeletonRow:
        item_id = str(item["id"])
        package = item_material.get(item_id)
        available = isinstance(package, str) and bool(package)
        return CurriculumSkeletonRow(
            item_id=item_id,
            status=str(item["status"]),
            material="available" if available else "preparing",
            title=str(item["title"]),
            visible_id=item_id,
            visible_status=str(item["status"]),
            state_kind="link" if available else "pending",
            state_href=(
                _curriculum_relative_href(current, f"units/{package}/index.html")
                if available
                else ""
            ),
            state_text="教材を読む" if available else "準備中",
        )

    for family in families:
        if not isinstance(family, dict):
            errors.append("canonical curriculum family is invalid")
            continue
        path = str(family["href"])
        raw = html_by_path.get(path)
        if raw is None:
            errors.append(f"curriculum family skeleton page missing: {family['slug']}")
            continue
        if 'class="page-curriculum-family"' not in raw:
            errors.append(f"curriculum family page class missing: {family['slug']}")
        expected_items = sorted([
            item
            for item in units
            if isinstance(item, dict)
            and str(item.get("id", "")).startswith(str(family["unit_id_prefix"]))
        ], key=lambda item: str(item["id"]))
        expected_groups: Dict[str, List[Dict[str, str]]] = {}
        for item in expected_items:
            expected_groups.setdefault(str(item["grade"]), []).append(item)
        expected_tracks = tuple(
            CurriculumSkeletonTrack(
                label=grade,
                count=len(group_items),
                count_unit="単元",
                status_summary=curriculum_status_summary_from_items(group_items),
                rows=tuple(
                    expected_row(item, PurePosixPath(path)) for item in group_items
                ),
            )
            for grade, group_items in expected_groups.items()
        )
        expected_family_label = str(family["label"])
        expected_state_label = (
            "教材あり" if family["availability"] == "available" else "準備中"
        )
        expected_status_summary = curriculum_status_summary_from_items(expected_items)
        page_fragments = (
            f"<h1>{html_module.escape(expected_family_label)}</h1>",
            f'class="curriculum-availability is-{family["availability"]}">{expected_state_label}</span>',
            f"正本の進捗表にある{len(expected_items)}単元を、学校段階・学年ごとに並べています。教材の掲載有無と制作工程の状態は別の情報です。",
            f'<p class="curriculum-hero-meta">正本の状態: {html_module.escape(expected_status_summary)}</p>',
        )
        if any(fragment not in raw for fragment in page_fragments):
            errors.append(f"curriculum family page summary differs: {family['slug']}")
        actual_tracks = collect_curriculum_skeleton_tracks(raw)
        if actual_tracks != expected_tracks:
            errors.append(f"curriculum family visible skeleton differs: {family['slug']}")
        actual_row_list = [row for track in actual_tracks for row in track.rows]
        declared_row_ids = re.findall(r'data-curriculum-id="([^"]+)"', raw)
        parsed_row_ids = [row.item_id for row in actual_row_list]
        if declared_row_ids != parsed_row_ids:
            errors.append(f"curriculum family has stray or malformed rows: {family['slug']}")
        actual_rows = {row.item_id: (row.status, row.material) for row in actual_row_list}
        expected_rows = {
            row.item_id: (row.status, row.material)
            for track in expected_tracks
            for row in track.rows
        }
        if len(actual_row_list) != len(actual_rows):
            errors.append(f"curriculum family skeleton has duplicate rows: {family['slug']}")
        if actual_rows != expected_rows:
            errors.append(f"curriculum family skeleton rows differ: {family['slug']}")
        if family["availability"] == "preparing":
            preparing_explanation = (
                f"正本の進捗表には{len(expected_items)}単元が登録されていますが、"
                "このサイトで読める教材本文はまだありません。"
                "完成時期や制作中であることを示す表示ではありません。"
            )
            note_match = re.search(
                r'<section class="container curriculum-preparing-note"[^>]*>(.*?)</section>',
                raw,
                re.DOTALL,
            )
            expected_note_text = (
                "NOT YET PUBLISHED 教材は準備中です " + preparing_explanation
            )
            if (
                note_match is None
                or _curriculum_visible_text(note_match.group(1)) != expected_note_text
                or any(
                    forbidden in raw
                    for forbidden in ("現在制作中", "まもなく完成", "完成予定です")
                )
            ):
                errors.append(f"preparing family explanation missing: {family['slug']}")
            if any(material == "available" for _status, material in actual_rows.values()):
                errors.append(f"preparing family contains false material link: {family['slug']}")
        else:
            available_summary = (
                f"{family['packages']}パッケージ、レッスン・本文"
                f"{family['lesson_pages']}ページを掲載しています。"
            )
            if available_summary not in raw:
                errors.append(f"available family summary differs: {family['slug']}")

    modules_path = "curriculum/modules/index.html"
    modules_raw = html_by_path.get(modules_path)
    if modules_raw is None:
        errors.append("curriculum modules skeleton page missing")
    else:
        expected_module_tracks: List[CurriculumSkeletonTrack] = []
        for subject in ("数学", "英語", "国語", "理科", "社会"):
            subject_items = [
                item
                for item in modules
                if isinstance(item, dict) and str(item.get("subject", "")) == subject
            ]
            if subject_items:
                expected_module_tracks.append(
                    CurriculumSkeletonTrack(
                        label=subject,
                        count=len(subject_items),
                        count_unit="件",
                        status_summary=curriculum_status_summary_from_items(subject_items),
                        rows=tuple(
                            expected_row(item, PurePosixPath(modules_path))
                            for item in subject_items
                        ),
                    )
                )
        actual_module_tracks = collect_curriculum_skeleton_tracks(modules_raw)
        if actual_module_tracks != tuple(expected_module_tracks):
            errors.append("curriculum modules visible skeleton differs")
        actual_module_list = [
            row for track in actual_module_tracks for row in track.rows
        ]
        declared_module_ids = re.findall(
            r'data-curriculum-id="([^"]+)"', modules_raw
        )
        parsed_module_ids = [row.item_id for row in actual_module_list]
        if declared_module_ids != parsed_module_ids:
            errors.append("curriculum modules have stray or malformed rows")
        actual_modules = {
            row.item_id: (row.status, row.material) for row in actual_module_list
        }
        expected_modules = {
            row.item_id: (row.status, row.material)
            for track in expected_module_tracks
            for row in track.rows
        }
        if len(actual_module_list) != len(actual_modules):
            errors.append("curriculum modules skeleton has duplicate rows")
        if actual_modules != expected_modules:
            errors.append("curriculum modules skeleton rows differ")
        actual_module_grades: Dict[str, str] = {}
        for item_id, row_body in re.findall(
            r'<li data-curriculum-id="([^"]+)"[^>]*>(.*?)</li>',
            modules_raw,
            re.DOTALL,
        ):
            grade_match = re.search(
                r'<span class="curriculum-unit-grade">(.*?)</span>',
                row_body,
                re.DOTALL,
            )
            actual_module_grades[html_module.unescape(item_id)] = (
                _curriculum_visible_text(grade_match.group(1))
                if grade_match is not None
                else ""
            )
        expected_module_grades = {
            str(item["id"]): str(item["grade"])
            for item in modules
            if isinstance(item, dict)
        }
        if actual_module_grades != expected_module_grades:
            errors.append("curriculum modules visible grades differ")
        module_summary = (
            f"公開コア単元とは別枠の{len(modules)}件です。"
            "名称・学校段階・状態を正本の進捗表から表示します。"
        )
        if module_summary not in modules_raw:
            errors.append("curriculum modules page summary differs")
    curriculum_index_raw = html_by_path.get("curriculum/index.html")
    if curriculum_index_raw is None:
        errors.append("curriculum index page missing")
    else:
        expected_index_fragments = (
            f"<h2 id=\"curriculum-grid-title\">{len(families)}の教科入口</h2>",
            f"公開コア{len(units)}単元とは別に、正本には{len(modules)}件の科目モジュールが登録されています。",
        )
        if any(fragment not in curriculum_index_raw for fragment in expected_index_fragments):
            errors.append("curriculum index visible totals differ")
    return errors


def decode_css_escapes(value: str) -> str:
    """Independently normalize CSS escapes in generated SVG attributes."""
    output: List[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            output.append(value[index])
            index += 1
            continue
        index += 1
        if index >= len(value):
            output.append("�")
            break
        if value[index] in "\r\n\f":
            if value[index] == "\r" and index + 1 < len(value) and value[index + 1] == "\n":
                index += 1
            index += 1
            continue
        match = re.match(r"[0-9a-fA-F]{1,6}", value[index:])
        if match:
            codepoint = int(match.group(0), 16)
            output.append(chr(codepoint) if 0 < codepoint <= 0x10FFFF else "�")
            index += len(match.group(0))
            if index < len(value) and value[index] in " \t\r\n\f":
                if value[index] == "\r" and index + 1 < len(value) and value[index + 1] == "\n":
                    index += 1
                index += 1
            continue
        output.append(value[index])
        index += 1
    return "".join(output)


def normalized_css_value(value: str) -> str:
    return re.sub(r"/\*.*?\*/", "", decode_css_escapes(value), flags=re.DOTALL)


def source_progress_statuses(source: Path) -> Dict[str, str]:
    """Independently read canonical package states for review-conflict checks."""
    progress = source / "curriculum/PROGRESS_INDEX.md"
    found: Dict[str, str] = {}
    for line in progress.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[2].startswith("`") and cells[2].endswith("`"):
            found[cells[2].strip("`")] = re.sub(r"[*_`]", "", cells[3]).strip()
    return found


def source_package_status(slug: str, statuses: Dict[str, str]) -> str:
    if slug in statuses:
        return statuses[slug]
    children = {
        status for unit_id, status in statuses.items() if unit_id.startswith(slug + "--")
    }
    if len(children) == 1:
        return children.pop()
    if children:
        order = {
            label: index
            for index, label in enumerate(
                ("未着手", "調査済", "ドラフト", "QA済", "外部レビュー済", "人間レビュー済", "公開済")
            )
        }
        return min(children, key=lambda label: order.get(label, -1))
    return "候補ドラフト"


def expected_review_state_conflicts(source: Path) -> Dict[str, str]:
    """Derive stale draft labels from source, not from the generated report."""
    statuses = source_progress_statuses(source)
    expected: Dict[str, str] = {}
    for markdown in sorted((source / "materials").rglob("*.md")):
        rel = markdown.relative_to(source)
        if len(rel.parts) < 4:
            continue
        status = source_package_status(rel.parts[2], statuses)
        if status not in {"人間レビュー済", "公開済"}:
            continue
        text = markdown.read_text(encoding="utf-8")
        if any(marker in text for marker in ("候補ドラフト", "人間レビュー前", "最終レビューはこれから")):
            expected[rel.as_posix()] = status
    return expected


def review_state_contract_matches(
    expected: Dict[str, str], reported: object, notice_sources: Set[str]
) -> bool:
    if not isinstance(reported, list):
        return False
    report_map: Dict[str, str] = {}
    for item in reported:
        if not isinstance(item, dict):
            return False
        source = item.get("source")
        status = item.get("registry_status")
        if not isinstance(source, str) or not isinstance(status, str) or source in report_map:
            return False
        report_map[source] = status
    return report_map == expected and notice_sources == set(expected)


def is_public_update_source_path(value: str) -> bool:
    if value in {"README.md", "NOTICE.md", "curriculum/PROGRESS_INDEX.md"}:
        return True
    if value.startswith("curriculum/registry/") and value.endswith(".md"):
        return True
    if value.startswith("docs/assets/") or value.startswith("materials/"):
        return Path(value).suffix.lower() in UPDATE_FILE_SUFFIXES
    return False


def canonical_public_update_commits(
    source: Path,
    start_commit: str,
    limit: int = 50,
) -> Tuple[List[str], bool, bool]:
    """Independently derive public-impact commits against each first parent."""
    shallow = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--is-shallow-repository"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    rows = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "rev-list",
            "--first-parent",
            "--parents",
            start_commit,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    expected: List[str] = []
    for row in rows:
        commit_and_parents = row.split()
        if not commit_and_parents or not re.fullmatch(r"[0-9a-f]{40}", commit_and_parents[0]):
            raise ValueError("invalid first-parent Git row")
        commit = commit_and_parents[0]
        if len(commit_and_parents) > 1:
            command = [
                "git",
                "-C",
                str(source),
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                commit_and_parents[1],
                commit,
            ]
        else:
            command = [
                "git",
                "-C",
                str(source),
                "diff-tree",
                "--root",
                "--no-renames",
                "-z",
                "--no-commit-id",
                "--name-only",
                "-r",
                commit,
            ]
        changed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\x00")
        if any(is_public_update_source_path(value) for value in changed):
            expected.append(commit)
        if len(expected) > limit:
            return expected[:limit], True, shallow == "false"
    return expected, False, shallow == "false"


@dataclass
class ParsedHtml:
    path: Path
    doctype_present: bool = False
    html_lang_ja: bool = False
    title: str = ""
    descriptions: List[str] = field(default_factory=list)
    has_viewport: bool = False
    has_main: bool = False
    has_focusable_main_target: bool = False
    has_footer: bool = False
    has_skip_link: bool = False
    has_internal_stylesheet: bool = False
    has_internal_icon: bool = False
    csp_content: str = ""
    has_site_nav_container: bool = False
    ids: Set[str] = field(default_factory=set)
    duplicate_ids: Set[str] = field(default_factory=set)
    classes: Set[str] = field(default_factory=set)
    tags: Set[str] = field(default_factory=set)
    links: List[Tuple[str, str, str]] = field(default_factory=list)
    inline_script_exists: bool = False
    inline_svg_nodes: List[Tuple[str, str, str, str, str, str]] = field(default_factory=list)
    svg_title_ids: Set[str] = field(default_factory=set)
    svg_desc_ids: Set[str] = field(default_factory=set)
    table_region_labels: List[str] = field(default_factory=list)
    table_header_count: int = 0
    scoped_table_header_count: int = 0
    answer_link_count: int = 0
    empty_state_is_live: bool = False
    main_text: str = ""
    raw_text: str = ""
    source_sha256_attr: Optional[str] = None
    canonical_hrefs: List[str] = field(default_factory=list)
    robots_values: List[str] = field(default_factory=list)
    og_urls: List[str] = field(default_factory=list)
    og_images: List[str] = field(default_factory=list)
    mathml_count: int = 0
    in_title: bool = False
    in_main: bool = False
    in_paragraph: bool = False
    block_in_paragraph: bool = False
    unmarked_english_passages: List[str] = field(default_factory=list)


class HtmlCollector(HTMLParser):
    def __init__(self, path: Path):
        super().__init__(convert_charrefs=True)
        self.data = ParsedHtml(path=path)
        self.svg_depth = 0
        self.lang_en_depth = 0
        self.excluded_text_depth = 0
        self.element_stack: List[Tuple[str, bool, bool]] = []

    def load(self) -> ParsedHtml:
        text = self.data.path.read_text(encoding="utf-8")
        self.data.raw_text = text
        self.feed(text)
        return self.data

    def handle_decl(self, decl: str) -> None:
        if decl.strip().lower().startswith("doctype"):
            self.data.doctype_present = True

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        attrs_dict = {k: (v or "") for k, v in attrs}
        lang_en = attrs_dict.get("lang", "").lower().startswith("en")
        excluded_text = tag in {"code", "pre", "script", "style", "svg"}
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.element_stack.append((tag, lang_en, excluded_text))
            self.lang_en_depth += int(lang_en)
            self.excluded_text_depth += int(excluded_text)
        self.data.tags.add(tag)
        if tag == "svg":
            self.svg_depth += 1
        elif self.svg_depth and tag == "title" and attrs_dict.get("id"):
            self.data.svg_title_ids.add(attrs_dict["id"])
        elif self.svg_depth and tag == "desc" and attrs_dict.get("id"):
            self.data.svg_desc_ids.add(attrs_dict["id"])

        if tag == "p":
            self.data.in_paragraph = True
        elif self.data.in_paragraph and tag in {"figure", "aside", "div", "table", "nav", "ol", "ul"}:
            self.data.block_in_paragraph = True

        if cid := attrs_dict.get("id"):
            if cid in self.data.ids:
                self.data.duplicate_ids.add(cid)
            self.data.ids.add(cid)
        cls = attrs_dict.get("class", "")
        class_tokens = set(cls.split())
        if cls:
            self.data.classes.update(class_tokens)

        if {"site-nav", "container"}.issubset(class_tokens):
            self.data.has_site_nav_container = True
        if tag == "a" and "answer-link" in class_tokens:
            self.data.answer_link_count += 1
        if "table-wrap" in class_tokens and attrs_dict.get("role") == "region":
            self.data.table_region_labels.append(attrs_dict.get("aria-label", ""))
        if tag == "th":
            self.data.table_header_count += 1
            if attrs_dict.get("scope") in {"col", "row"}:
                self.data.scoped_table_header_count += 1
        if "empty-state" in class_tokens and (
            attrs_dict.get("role") == "status" or attrs_dict.get("aria-live")
        ):
            self.data.empty_state_is_live = True

        if tag == "html":
            lang = attrs_dict.get("lang", "").lower()
            self.data.html_lang_ja = lang.startswith("ja")
        elif tag == "title" and not self.svg_depth:
            self.data.in_title = True
        elif tag == "meta" and attrs_dict.get("name", "").lower() == "viewport":
            self.data.has_viewport = True
        elif tag == "main":
            self.data.has_main = True
            self.data.in_main = True
            if (
                attrs_dict.get("id") == "main-content"
                and attrs_dict.get("tabindex") == "-1"
            ):
                self.data.has_focusable_main_target = True
        elif tag == "footer":
            self.data.has_footer = True
        elif tag == "a" and (
            attrs_dict.get("id") == "skip-link" or "skip-link" in attrs_dict.get("class", "").split()
        ):
            self.data.has_skip_link = True
            href = attrs_dict.get("href", "").strip()
            if href:
                self.data.links.append(("a", "href", href))
        elif tag == "link":
            href = attrs_dict.get("href", "").strip()
            if href:
                rel = attrs_dict.get("rel", "").lower().split()
                if "stylesheet" in rel:
                    self.data.has_internal_stylesheet = True
                if "icon" in rel:
                    self.data.has_internal_icon = True
                if "canonical" in rel:
                    self.data.canonical_hrefs.append(href)
                else:
                    self.data.links.append(("link", "href", href))
        elif tag == "meta":
            name = attrs_dict.get("name", "").lower()
            http_equiv = attrs_dict.get("http-equiv", "").lower()
            property_name = attrs_dict.get("property", "").lower()
            content = attrs_dict.get("content", "").strip()
            if http_equiv == "content-security-policy":
                self.data.csp_content = content
            elif name == "description":
                self.data.descriptions.append(content)
            elif name == "robots":
                self.data.robots_values.append(content)
            elif property_name == "og:url":
                self.data.og_urls.append(content)
            elif property_name == "og:image":
                self.data.og_images.append(content)
        elif tag == "a":
            href = attrs_dict.get("href", "").strip()
            if href:
                self.data.links.append(("a", "href", href))
        elif tag == "script":
            src = attrs_dict.get("src", "").strip()
            if src:
                self.data.links.append(("script", "src", src))
            else:
                self.data.inline_script_exists = True
        elif tag == "img":
            src = attrs_dict.get("src", "").strip()
            if src:
                self.data.links.append(("img", "src", src))
        elif tag == "iframe":
            self.data.tags.add("iframe")
            src = attrs_dict.get("src", "").strip()
            if src:
                self.data.links.append(("iframe", "src", src))
        elif tag == "video":
            self.data.tags.add("video")
            src = attrs_dict.get("src", "").strip()
            if src:
                self.data.links.append(("video", "src", src))
        elif tag == "audio":
            self.data.tags.add("audio")
            src = attrs_dict.get("src", "").strip()
            if src:
                self.data.links.append(("audio", "src", src))
        elif tag == "form":
            self.data.tags.add("form")
        elif tag == "canvas":
            self.data.tags.add("canvas")
        elif tag == "body":
            sha = attrs_dict.get("data-source-sha256", "").strip()
            if sha:
                self.data.source_sha256_attr = sha
        elif tag == "svg":
            self.data.inline_svg_nodes.append(
                (
                    attrs_dict.get("viewBox", "") or attrs_dict.get("viewbox", ""),
                    attrs_dict.get("role", ""),
                    attrs_dict.get("aria-label", ""),
                    attrs_dict.get("aria-labelledby", ""),
                    attrs_dict.get("aria-describedby", ""),
                    attrs_dict.get("class", ""),
                )
            )
        elif tag == "math":
            self.data.mathml_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.data.in_title:
            self.data.in_title = False
        elif tag == "main":
            self.data.in_main = False
        elif tag == "p":
            self.data.in_paragraph = False
        if tag == "svg":
            self.svg_depth = max(0, self.svg_depth - 1)
        for index in range(len(self.element_stack) - 1, -1, -1):
            if self.element_stack[index][0] == tag:
                removed = self.element_stack[index:]
                del self.element_stack[index:]
                self.lang_en_depth -= sum(int(item[1]) for item in removed)
                self.excluded_text_depth -= sum(int(item[2]) for item in removed)
                break

    def handle_data(self, data: str) -> None:
        if self.data.in_title:
            self.data.title += data
        if self.data.in_main:
            # Keep text-node boundaries so adjacent table/SVG nodes cannot
            # accidentally merge separate numeric tokens during fidelity checks.
            self.data.main_text += " " + data
            if self.lang_en_depth == 0 and self.excluded_text_depth == 0:
                self.data.unmarked_english_passages.extend(
                    match.group(0)[:120] for match in ENGLISH_PASSAGE.finditer(data)
                )


def read_build_report(path: Path) -> Dict:
    if not path.exists():
        raise FileNotFoundError(f"build-report.json not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_site_config() -> Dict[str, str]:
    path = Path(__file__).resolve().parent / "site.config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = {"base_url", "og_image_output"}
    missing = sorted(required - raw.keys())
    if missing or any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in required):
        raise ValueError("site.config.jsonの必須値が不足しています: " + ", ".join(missing))
    config = {key: str(value).strip() for key, value in raw.items() if isinstance(value, str)}
    parsed = urlsplit(config["base_url"])
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("site.config.jsonのbase_urlはquery/fragmentなしのHTTPS URLにしてください")
    if not config["base_url"].endswith("/"):
        raise ValueError("site.config.jsonのbase_urlは末尾スラッシュが必要です")
    output = Path(config["og_image_output"])
    if output.is_absolute() or ".." in output.parts:
        raise ValueError("site.config.jsonのog_image_outputは公開root内の相対パスにしてください")
    return config


def public_url(base_url: str, relative: Path) -> str:
    value = relative.as_posix()
    if value == "index.html":
        value = ""
    elif value.endswith("/index.html"):
        value = value[: -len("index.html")]
    return urllib.parse.urljoin(base_url, value)


def resolve_source_root(
    site_root: Path,
    explicit: Optional[Path],
    build_report: Dict[str, object],
) -> Optional[Path]:
    """Resolve the source without baking a machine-local path into this checker."""
    candidates: List[Path] = []
    if explicit is not None:
        try:
            resolved = explicit.resolve()
        except OSError:
            return None
        return (
            resolved
            if (resolved / "materials").is_dir() and (resolved / ".git").exists()
            else None
        )
    environment_source = os.environ.get("MANABIGRID_SOURCE_ROOT")
    if environment_source:
        candidates.append(Path(environment_source))
    parent = site_root.parent
    candidates.append(parent / "manabigrid")
    candidates.extend(sorted(parent.glob("manabigrid_public_staging*/manabigrid")))
    source_info = build_report.get("source")
    if isinstance(source_info, dict) and isinstance(source_info.get("root"), str):
        candidates.append(Path(source_info["root"]))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if (resolved / "materials").is_dir() and (resolved / ".git").exists():
            return resolved
    return None


def validate_public_metadata(
    site_root: Path,
    parsed_pages: Dict[Path, ParsedHtml],
    build_report: Dict[str, object],
    errors: List[str],
    checks: List[Dict[str, object]],
) -> None:
    """Validate deploy-specific metadata from the single site configuration."""
    try:
        config = load_site_config()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"site config invalid: {exc}")
        return
    base_url = config["base_url"]
    og_image_relative = Path(config["og_image_output"])
    expected_og_image = public_url(base_url, og_image_relative)
    expected_sitemap_urls = {
        public_url(base_url, path.relative_to(site_root))
        for path in parsed_pages
        if path.relative_to(site_root).as_posix() != "404.html"
    }
    metadata_ok = True
    for path, parsed in parsed_pages.items():
        relative = path.relative_to(site_root)
        expected_url = public_url(base_url, relative)
        is_404 = relative.as_posix() == "404.html"
        expected_canonical = [] if is_404 else [expected_url]
        expected_robots = "noindex, follow" if is_404 else "index, follow"
        if parsed.canonical_hrefs != expected_canonical:
            errors.append(f"canonical mismatch: {relative}")
            metadata_ok = False
        if parsed.og_urls != [expected_url]:
            errors.append(f"og:url mismatch: {relative}")
            metadata_ok = False
        if parsed.og_images != [expected_og_image]:
            errors.append(f"og:image mismatch: {relative}")
            metadata_ok = False
        if parsed.robots_values != [expected_robots]:
            errors.append(f"robots meta mismatch: {relative}")
            metadata_ok = False
    checks.append({"name": "metadata:canonical_og_robots", "pass": metadata_ok})

    uniqueness_errors = metadata_uniqueness_errors(site_root, parsed_pages)
    checks.append(
        {
            "name": "metadata:unique_titles_descriptions",
            "pass": not uniqueness_errors,
        }
    )
    errors.extend(uniqueness_errors)

    nojekyll = site_root / ".nojekyll"
    checks.append({"name": "public:nojekyll", "pass": nojekyll.is_file() and nojekyll.read_bytes() == b""})
    if not nojekyll.is_file() or nojekyll.read_bytes() != b"":
        errors.append(".nojekyll is missing or not empty")

    image_path = site_root / og_image_relative
    image_ok = image_path.is_file() and image_path.read_bytes().startswith(PNG_SIGNATURE)
    report_image = None
    features = build_report.get("features")
    if isinstance(features, dict):
        report_image = features.get("og_image")
    if not isinstance(report_image, dict):
        image_ok = False
        errors.append("build-report missing features.og_image")
    elif report_image.get("output") != og_image_relative.as_posix():
        image_ok = False
        errors.append("build-report og image output mismatch")
    elif image_path.is_file() and report_image.get("sha256") != sha256_file(image_path):
        image_ok = False
        errors.append("build-report og image sha256 mismatch")
    checks.append({"name": "public:og_image", "pass": image_ok})
    if not image_ok and not any("og image" in error for error in errors):
        errors.append("OG image is missing or not a PNG")

    robots_path = site_root / "robots.txt"
    robots_ok = False
    if robots_path.is_file():
        robots_text = robots_path.read_text(encoding="utf-8")
        robots_ok = (
            "User-agent: *" in robots_text
            and "Allow: /" in robots_text
            and f"Sitemap: {public_url(base_url, Path('sitemap.xml'))}" in robots_text
        )
    checks.append({"name": "public:robots_txt", "pass": robots_ok})
    if not robots_ok:
        errors.append("robots.txt contract mismatch")

    sitemap_ok = False
    sitemap_path = site_root / "sitemap.xml"
    if sitemap_path.is_file():
        try:
            root = ET.fromstring(sitemap_path.read_text(encoding="utf-8"))
            locations = {
                (node.text or "").strip()
                for node in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            }
            sitemap_ok = locations == expected_sitemap_urls
        except (ET.ParseError, OSError, UnicodeDecodeError):
            sitemap_ok = False
    checks.append({"name": "public:sitemap", "pass": sitemap_ok, "urls": len(expected_sitemap_urls)})
    if not sitemap_ok:
        errors.append("sitemap.xml contract mismatch")

    mathml_pages = [
        path.relative_to(site_root).as_posix()
        for path, parsed in parsed_pages.items()
        if parsed.mathml_count
    ]
    mathml_ok = len(mathml_pages) == 1 and sum(parsed_pages[site_root / page].mathml_count for page in mathml_pages) == 1
    checks.append({"name": "content:mathml_single_prototype", "pass": mathml_ok, "pages": mathml_pages})
    if not mathml_ok:
        errors.append("MathML prototype count must be exactly one")


def metadata_uniqueness_errors(
    site_root: Path,
    parsed_pages: Dict[Path, ParsedHtml],
) -> List[str]:
    """Require one useful, unambiguous title and description per indexable page."""
    errors: List[str] = []
    titles: Dict[str, List[str]] = {}
    descriptions: Dict[str, List[str]] = {}
    for path, parsed in parsed_pages.items():
        if parsed.robots_values != ["index, follow"]:
            continue
        relative = path.relative_to(site_root).as_posix()
        title = parsed.title.strip()
        if not title:
            errors.append(f"indexable page title is empty: {relative}")
        else:
            titles.setdefault(title, []).append(relative)
        if len(parsed.descriptions) != 1 or not parsed.descriptions[0].strip():
            errors.append(f"indexable page must have one description: {relative}")
        else:
            descriptions.setdefault(parsed.descriptions[0].strip(), []).append(relative)

    for value, paths in sorted(titles.items()):
        if len(paths) > 1:
            errors.append(
                "duplicate indexable title: "
                + ", ".join(paths)
                + f" ({value[:80]})"
            )
    for value, paths in sorted(descriptions.items()):
        if len(paths) > 1:
            errors.append(
                "duplicate indexable description: "
                + ", ".join(paths)
                + f" ({value[:80]})"
            )
    return errors


def theme_storage_contract_errors(
    theme_js_text: str,
    other_storage_users: List[str],
) -> List[str]:
    """Keep the sole persisted preference to one exact key and three exact values."""
    errors: List[str] = []
    storage_declarations = re.findall(
        r"\bconst\s+STORAGE_KEY\s*=\s*(['\"])([^'\"]+)\1\s*;",
        theme_js_text,
    )
    if storage_declarations != [("'", "manabigrid-theme")]:
        errors.append("theme storage key declaration is not exact")

    mode_declarations = re.findall(
        r"\bconst\s+VALID_MODES\s*=\s*new\s+Set\(\[([^\]]*)\]\)\s*;",
        theme_js_text,
    )
    normalized_modes = (
        re.sub(r"\s+", "", mode_declarations[0]) if len(mode_declarations) == 1 else ""
    )
    if normalized_modes != "'system','light','dark'":
        errors.append("theme modes are not exactly system, light, dark")

    accesses = [
        (method, re.sub(r"\s+", "", arguments))
        for method, arguments in re.findall(
            r"(?:window\.)?localStorage\.(getItem|setItem|removeItem)\s*\(([^)]*)\)",
            theme_js_text,
        )
    ]
    expected_accesses = [
        ("getItem", "STORAGE_KEY"),
        ("removeItem", "STORAGE_KEY"),
        ("setItem", "STORAGE_KEY,mode"),
    ]
    if accesses != expected_accesses:
        errors.append("theme storage access arguments are not exact")
    if theme_js_text.lower().count("localstorage") != len(expected_accesses):
        errors.append("theme script contains an unrecognized localStorage access")
    if theme_js_text.count("STORAGE_KEY") != 1 + len(expected_accesses):
        errors.append("theme storage key is used outside its exact declaration and accesses")
    if theme_js_text.count("VALID_MODES.has(saved)") != 1:
        errors.append("stored theme value is not checked against the exact modes")
    if theme_js_text.count("VALID_MODES.has(select.value)") != 1:
        errors.append("selected theme value is not checked against the exact modes")
    if other_storage_users:
        errors.append("another public script uses localStorage")
    return errors


def validate_external_link_appendix(
    site_root: Path,
    errors: List[str],
    warnings: List[str],
    report_path: Optional[Path] = None,
) -> Dict[str, object]:
    report_path = (report_path or site_root / "external-link-report.json").resolve()
    if not report_path.is_file():
        warnings.append("external-link-report.json is absent; live external checks were not run")
        return {"status": "not_run", "report_present": False}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"external link report invalid: {exc}")
        return {"status": "invalid", "report_present": True}
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        errors.append("external link report missing results")
        return {"status": "invalid", "report_present": True}
    hard_broken = [item for item in report["results"] if isinstance(item, dict) and item.get("classification") == "hard_broken"]
    if hard_broken:
        errors.append(f"external link report has {len(hard_broken)} hard broken URL(s)")
    return report


def normalize_local_target(raw: str, page: Path, site_root: Path) -> Optional[Path]:
    parsed = urlsplit(raw)
    if parsed.scheme:
        return None
    if not parsed.path:
        return None

    rel = unquote(parsed.path)
    if rel.startswith("/"):
        rel = rel.lstrip("/")
        try:
            base_path = urlsplit(load_site_config()["base_url"]).path.strip("/")
        except (OSError, ValueError, json.JSONDecodeError):
            base_path = ""
        if base_path and (rel == base_path or rel.startswith(base_path + "/")):
            rel = rel[len(base_path) :].lstrip("/")
        candidate = site_root / rel
    else:
        current_rel = page.relative_to(site_root).parent
        rel = PurePosixPath(current_rel.as_posix()) / rel
        candidate = site_root / rel

    candidate = candidate.resolve()
    try:
        candidate.relative_to(site_root.resolve())
    except ValueError:
        return None
    if str(raw).endswith("/"):
        candidate = candidate / "index.html"
    elif candidate.suffix == "":
        if candidate.is_dir():
            candidate = candidate / "index.html"
        elif (candidate.with_name(candidate.name + ".html")).exists():
            candidate = candidate.with_name(candidate.name + ".html")

    if candidate.exists():
        return candidate
    if (candidate / "index.html").exists():
        return candidate / "index.html"
    return None


def validate_link(
    source_page: Path,
    tag: str,
    attr: str,
    value: str,
    site_root: Path,
    id_map: Dict[Path, Set[str]],
) -> Tuple[str, str, Optional[Path]]:
    parsed = urlsplit(value)
    # HTML href fragments are URL-encoded while parsed element ids are decoded
    # Unicode strings.  Compare the same representation on both sides.
    fragment = unquote(parsed.fragment)
    if parsed.scheme:
        if parsed.scheme.lower() in BAD_SCHEMES:
            return "error", f"disallowed scheme: {parsed.scheme}", None
        try:
            base = urlsplit(load_site_config()["base_url"])
        except (OSError, ValueError, json.JSONDecodeError):
            base = None
        if base and (parsed.scheme.lower(), parsed.netloc.lower()) == (
            base.scheme.lower(),
            base.netloc.lower(),
        ):
            local_value = parsed.path + (("?" + parsed.query) if parsed.query else "")
            if fragment:
                local_value += "#" + fragment
            target = normalize_local_target(local_value, source_page, site_root)
            if not target:
                return "error", f"site target not found: {value}", None
            if fragment and fragment not in id_map.get(target, set()):
                return "error", f"missing fragment: #{fragment}", target
            return "internal", "", target
        if parsed.netloc and tag == "a" and attr == "href":
            host = parsed.netloc.lower().split(":")[0]
            if host in ALLOWED_EXTERNAL_HOSTS:
                return "external", "", None
        return "error", f"external {attr} not allowed: {value}", None

    if value.strip() == "#":
        return "error", "hash-only link is not allowed", None
    if not parsed.path:
        if fragment:
            if fragment in id_map.get(source_page.resolve(), set()):
                return "internal", "", source_page.resolve()
            return "error", f"missing fragment: #{fragment}", source_page.resolve()
        return "error", "empty path", None

    target = normalize_local_target(value, source_page, site_root)
    if not target or not target.exists():
        return "error", f"target not found: {value}", None

    if attr == "src" and tag == "script":
        if target.name not in {"site.js", "search-index.js", "theme.js"} or "_assets" not in target.as_posix():
            return "error", "script source must be an approved _assets script", None

    if fragment and fragment not in id_map.get(target, set()):
        return "error", f"missing fragment: #{fragment}", target

    if tag in {"a", "link", "iframe", "video", "audio", "img", "script"}:
        if attr == "href" and parsed.scheme == "":
            return "internal", "", target

    return "external" if parsed.netloc else "internal", "", target


def field(item: Dict[str, object], names: List[str]) -> Optional[str]:
    for name in names:
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def numeric_tokens(value: str) -> Counter[str]:
    return Counter(re.findall(r"[0-9０-９]+(?:[.,．，][0-9０-９]+)*", value))


def source_visible_text(raw: str) -> str:
    """Remove Markdown-only controls while retaining visible source content."""
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end >= 0:
            raw = raw[end + 5 :]
    raw = re.sub(
        r"<!--\s*gen_nav:nav:start.*?<!--\s*gen_nav:nav:end\s*-->",
        "",
        raw,
        flags=re.DOTALL,
    )
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    raw = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)
    raw = re.sub(r"^\s*(?:>\s*)+", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"^\s*\d+[.)]\s+", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"^\s*[-+*]\s+", "", raw, flags=re.MULTILINE)
    return raw.replace("<u>", "").replace("</u>", "")


def check_css_rules(site_root: Path, errors: List[str], checks: List[Dict[str, object]]) -> None:
    css_files = [path for path in iter_public_files(site_root) if path.suffix.lower() == ".css"]
    if not css_files:
        errors.append("no css files found")
        return

    merged = "\n".join(c.read_text(encoding="utf-8").lower() for c in css_files)
    required = {
        "media_print": "@media print" in merged,
        "print_a4": bool(re.search(r"@page\s*\{[^}]*size\s*:\s*a4", merged)),
        "media_max_width": "@media (max-width" in merged,
        "focus_visible": ":focus-visible" in merged,
        "table_focus_visible": ".table-wrap:focus-visible" in merged,
        "prefers_reduced_motion": "prefers-reduced-motion" in merged,
        "no_gradients": "gradient(" not in merged,
        "no_decorative_shadows": "box-shadow" not in merged,
        "no_hover_lift": "translatey" not in merged,
    }
    for name, ok in required.items():
        checks.append({"name": f"css:{name}", "pass": ok})
        if not ok:
            errors.append(f"css missing {name}")
    for css_path in css_files:
        text = css_path.read_text(encoding="utf-8")
        if re.search(r"@import\b", text, re.IGNORECASE):
            errors.append(f"external-capable CSS @import is not allowed: {css_path.name}")
        if re.search(r"url\(\s*['\"]?(?:https?:)?//", text, re.IGNORECASE):
            errors.append(f"external CSS url is not allowed: {css_path.name}")


def freshness_message(built_commit: str, current_head: str) -> str | None:
    if not built_commit or not current_head or built_commit == current_head:
        return None
    return (
        "正本HEADがビルド時コミットから進んでいます: "
        f"build={built_commit[:12]} current={current_head[:12]}。再ビルドしてください"
    )


def reported_source_commit_contract_errors(
    built_commit: object,
    expected_source_sha: Optional[str],
) -> List[str]:
    """Bind publication checks to the exact independently approved source SHA."""

    if not isinstance(built_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}",
        built_commit,
    ):
        return ["build-report source commit is not a lowercase 40-character SHA"]
    if expected_source_sha is None:
        return []
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source_sha):
        return ["expected canonical source SHA is invalid"]
    if built_commit != expected_source_sha:
        return ["build-report source commit does not match expected canonical SHA"]
    return []


def normalize_repository_url(value: object) -> str:
    return str(value or "").strip().rstrip("/").removesuffix(".git")


def source_checkout_contract_errors(
    source_info: object,
    actual_status: Optional[str] = None,
    actual_origin: Optional[str] = None,
    actual_head: Optional[str] = None,
    expected_head: Optional[str] = None,
) -> List[str]:
    if not isinstance(source_info, dict):
        return ["build-report source metadata is invalid"]
    errors: List[str] = []
    if source_info.get("git_status_before") != "" or source_info.get("git_status_after") != "":
        errors.append("source checkout was not clean throughout build")
    if normalize_repository_url(source_info.get("repository")) != OFFICIAL_SOURCE_REPOSITORY:
        errors.append("reported source repository is not the official canonical repository")
    if normalize_repository_url(source_info.get("origin")) != OFFICIAL_SOURCE_REPOSITORY:
        errors.append("reported source origin is not the official canonical repository")
    if actual_status is not None and actual_status != "":
        errors.append("current source checkout has uncommitted changes")
    if (
        actual_origin is not None
        and normalize_repository_url(actual_origin) != OFFICIAL_SOURCE_REPOSITORY
    ):
        errors.append("current source origin is not the official canonical repository")
    if expected_head is not None and not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        errors.append("expected canonical source SHA is invalid")
    elif actual_head is not None and expected_head is not None and actual_head != expected_head:
        errors.append("current source HEAD is not the verified official main revision")
    return errors


def expected_markdown_sources(source_root: Path) -> Set[str]:
    """Return the complete canonical Markdown set that must be converted."""
    expected = {
        path.relative_to(source_root).as_posix()
        for path in (source_root / "materials").rglob("*.md")
        if path.is_file()
    }
    progress = source_root / "curriculum/PROGRESS_INDEX.md"
    if progress.is_file():
        expected.add(progress.relative_to(source_root).as_posix())
    return expected


def source_manifest_contract_errors(
    source_root: Path,
    source_files: object,
    markdown_report: object,
    site_root: Optional[Path] = None,
) -> List[str]:
    """Reject incomplete, duplicated, or self-reported-only conversion manifests."""
    expected = expected_markdown_sources(source_root)
    if not isinstance(source_files, list):
        return ["build-report source_files must be a list"]

    errors: List[str] = []
    sources: List[str] = []
    outputs: List[str] = []
    for index, item in enumerate(source_files):
        if not isinstance(item, dict):
            errors.append(f"source_files[{index}] must be an object")
            continue
        source = item.get("source")
        output = item.get("output")
        source_sha = item.get("sha256")
        if not isinstance(source, str) or not source:
            errors.append(f"source_files[{index}].source is invalid")
        else:
            sources.append(source)
        if not isinstance(output, str) or not output:
            errors.append(f"source_files[{index}].output is invalid")
        else:
            outputs.append(output)
            output_path = PurePosixPath(output)
            if (
                output_path.is_absolute()
                or ".." in output_path.parts
                or output_path.suffix != ".html"
            ):
                errors.append(f"source_files[{index}].output path is unsafe")
            elif site_root is not None and not (site_root / output).is_file():
                errors.append(f"source_files[{index}].output is missing")
        if not isinstance(source_sha, str) or not re.fullmatch(
            r"[0-9a-f]{64}", source_sha
        ):
            errors.append(f"source_files[{index}].sha256 is invalid")

    source_counts = Counter(sources)
    output_counts = Counter(outputs)
    duplicate_sources = sorted(
        source for source, count in source_counts.items() if count > 1
    )
    duplicate_outputs = sorted(
        output for output, count in output_counts.items() if count > 1
    )
    if duplicate_sources:
        errors.append(
            "source_files has duplicate sources: " + ", ".join(duplicate_sources[:5])
        )
    if duplicate_outputs:
        errors.append(
            "source_files has duplicate outputs: " + ", ".join(duplicate_outputs[:5])
        )

    actual = set(sources)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append(
            "source_files omits canonical Markdown: " + ", ".join(missing[:5])
        )
    if extra:
        errors.append(
            "source_files includes noncanonical Markdown: " + ", ".join(extra[:5])
        )

    if not isinstance(markdown_report, dict):
        errors.append("build-report markdown summary is invalid")
    else:
        expected_count = len(expected)
        if (
            markdown_report.get("expected") != expected_count
            or markdown_report.get("converted") != expected_count
            or markdown_report.get("failed") != []
            or len(source_files) != expected_count
        ):
            errors.append(
                "markdown summary does not match the independently enumerated canonical set"
            )
    return errors


def publication_site_contract_errors(
    build_report: object, expected_site_sha: Optional[str]
) -> List[str]:
    if not isinstance(build_report, dict):
        return ["build-report publication metadata is invalid"]
    publication = build_report.get("publication")
    if not isinstance(publication, dict):
        return ["build-report publication metadata is invalid"]
    reported = publication.get("site_commit")
    errors: List[str] = []
    if reported is not None and (
        not isinstance(reported, str) or not re.fullmatch(r"[0-9a-f]{40}", reported)
    ):
        errors.append("reported site commit is invalid")
    if expected_site_sha is not None:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_site_sha):
            errors.append("expected site commit is invalid")
        elif reported != expected_site_sha:
            errors.append("reported site commit does not match the expected release commit")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Static site checker for ManabiGrid.")
    parser.add_argument(
        "site_root",
        nargs="?",
        default=str(Path(__file__).resolve().parent),
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="正本root。省略時はMANABIGRID_SOURCE_ROOTと隣接候補から解決します",
    )
    parser.add_argument(
        "--expected-source-sha",
        help="workflow等が独立取得した正本mainの小文字40桁SHA",
    )
    parser.add_argument(
        "--expected-site-sha",
        help="公開workflowがbuild-reportへ埋め込むsite commitの小文字40桁SHA",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        help="check-report.jsonの出力先。自己検査時に公開候補を変更しないために使えます",
    )
    parser.add_argument(
        "--external-report",
        type=Path,
        help="外部URL検査レポートの読取先。省略時はsite_root/external-link-report.json",
    )
    parser.add_argument(
        "--self-test-freshness",
        action="store_true",
        help="鮮度不一致警告の分岐だけを自己検査する",
    )
    args = parser.parse_args()
    if args.self_test_freshness:
        mismatch = freshness_message("a" * 40, "b" * 40)
        match = freshness_message("a" * 40, "a" * 40)
        if mismatch and match is None:
            print(f"鮮度検査self-test: PASS（警告例: {mismatch}）")
            return 0
        print("鮮度検査self-test: FAIL")
        return 1
    site_root = Path(args.site_root).resolve()

    report_path = site_root / "build-report.json"
    report_out_path = (args.report_output or site_root / "check-report.json").resolve()

    errors: List[str] = []
    warnings: List[str] = []
    checks: List[Dict[str, object]] = []
    broken_links: List[Dict[str, object]] = []
    internal_links_checked = 0
    external_links_checked = 0
    source_files_checked = 0
    numeric_documents_checked = 0

    try:
        build_report = read_build_report(report_path)
    except Exception as exc:
        build_report = {}
        errors.append(str(exc))
    publication_errors = publication_site_contract_errors(
        build_report, args.expected_site_sha
    )
    errors.extend(publication_errors)
    checks.append(
        {
            "name": "publication:site_commit",
            "pass": not publication_errors,
            "expected": args.expected_site_sha,
            "reported": (
                build_report.get("publication", {}).get("site_commit")
                if isinstance(build_report.get("publication"), dict)
                else None
            ),
        }
    )

    public_files = iter_public_files(site_root)
    html_paths = sorted(path for path in public_files if path.suffix.lower() == ".html")
    parsed_pages: Dict[Path, ParsedHtml] = {}
    for html in html_paths:
        try:
            parsed_pages[html] = HtmlCollector(html).load()
        except Exception as exc:
            errors.append(f"parse error: {html}: {exc}")

    id_map: Dict[Path, Set[str]] = {path: parsed.ids for path, parsed in parsed_pages.items()}

    missing_public = missing_public_entries(site_root)
    if missing_public:
        errors.append("public allowlist entries missing: " + ", ".join(missing_public))
    quarantine_ok = quarantine_self_test()
    if not quarantine_ok:
        errors.append("public artifact quarantine self-test failed")
    quarantine_findings = quarantine_public_artifact(site_root)
    if quarantine_findings:
        errors.extend(
            "public artifact quarantine hit: "
            + f"{finding['path']} ({finding['scope']}: {', '.join(finding['issues'])})"
            for finding in quarantine_findings
        )
    checks.append(
        {
            "name": "public:allowlist_quarantine",
            "pass": not missing_public and quarantine_ok and not quarantine_findings,
            "files_checked": len(iter_public_files(site_root)),
            "findings": quarantine_findings,
            "scope": "allowlisted paths and UTF-8 text only; source history is not checked here",
        }
    )

    for path, parsed in parsed_pages.items():
        if not parsed.doctype_present:
            errors.append(f"missing doctype: {path.name}")
        if not parsed.html_lang_ja:
            errors.append(f"html lang not ja: {path.name}")
        if not parsed.title.strip():
            errors.append(f"title missing: {path.name}")
        if not parsed.has_viewport:
            errors.append(f"viewport missing: {path.name}")
        required_csp = {
            "default-src 'self'",
            "script-src 'self'",
            "connect-src 'none'",
            "object-src 'none'",
            "frame-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
        }
        if not required_csp.issubset(
            {directive.strip() for directive in parsed.csp_content.split(";") if directive.strip()}
        ):
            errors.append(f"restrictive CSP missing or incomplete: {path.name}")
        if not parsed.has_main:
            errors.append(f"main missing: {path.name}")
        if not parsed.has_focusable_main_target:
            errors.append(f"focusable main skip target missing: {path.name}")
        if not parsed.has_footer:
            errors.append(f"footer missing: {path.name}")
        if not parsed.has_skip_link:
            errors.append(f"skip-link missing: {path.name}")
        if not parsed.has_internal_stylesheet:
            errors.append(f"internal stylesheet missing: {path.name}")
        if not parsed.has_internal_icon:
            errors.append(f"internal favicon missing: {path.name}")
        if not parsed.has_site_nav_container:
            errors.append(f"site nav container contract missing: {path.name}")
        if "page-top" not in parsed.ids:
            errors.append(f"page-top target missing: {path.name}")
        if "page-end-nav" not in parsed.classes:
            errors.append(f"page-end navigation missing: {path.name}")
        if (
            "theme-control" not in parsed.classes
            or parsed.raw_text.count("data-theme-select") != 1
            or "_assets/theme.js" not in parsed.raw_text
        ):
            errors.append(f"theme control contract missing: {path.name}")

        figure_links = re.findall(
            r'<a\b[^>]*\bclass=["\'][^"\']*\bfigure-source\b[^"\']*["\'][^>]*>',
            parsed.raw_text,
            re.IGNORECASE,
        )
        if figure_links:
            if (
                "figure-dialog" not in parsed.classes
                or "dialog" not in parsed.tags
                or not all(
                    "data-figure-open" in opening
                    and re.search(r'\btarget=["\']_blank["\']', opening)
                    and re.search(r'\brel=["\'][^"\']*\bnoopener\b', opening)
                    for opening in figure_links
                )
                or "data-figure-close" not in parsed.raw_text
                or "data-figure-original" not in parsed.raw_text
                or parsed.raw_text.count('id="figure-dialog-title"') != 1
            ):
                errors.append(f"figure dialog or fallback contract missing: {path.name}")
        elif "figure-dialog" in parsed.classes:
            errors.append(f"unused figure dialog emitted: {path.name}")

        if parsed.inline_script_exists:
            errors.append(f"inline script not allowed: {path.name}")
        if parsed.duplicate_ids:
            errors.append(
                f"duplicate ids in {path.name}: {', '.join(sorted(parsed.duplicate_ids))}"
            )
        for fragment_id in re.findall(r"url\(#([^)]+)\)", parsed.raw_text):
            if fragment_id not in parsed.ids:
                errors.append(f"missing inline SVG fragment #{fragment_id}: {path.name}")
        for opening in re.findall(r"<svg\b[^>]*>", parsed.raw_text, re.IGNORECASE):
            for attribute in (
                "class",
                "role",
                "focusable",
                "aria-label",
                "aria-labelledby",
                "aria-describedby",
            ):
                if len(re.findall(rf"\s{attribute}\s*=", opening, re.IGNORECASE)) > 1:
                    errors.append(f"duplicate SVG {attribute} attribute: {path.name}")
        for svg_fragment in re.findall(
            r"<svg\b.*?</svg>", parsed.raw_text, re.IGNORECASE | re.DOTALL
        ):
            if re.search(
                r"<script\b|<style\b|<foreignObject\b|\son[a-z]+\s*=|@import|url\((?!\s*#)",
                svg_fragment,
                re.IGNORECASE,
            ):
                errors.append(f"active or remote-loading inline SVG is not allowed: {path.name}")
                break
        if parsed.block_in_paragraph:
            errors.append(
                "block element nested in paragraph: "
                + str(path.relative_to(site_root))
            )
        if parsed.table_region_labels and (
            any(not label for label in parsed.table_region_labels)
            or len(parsed.table_region_labels) != len(set(parsed.table_region_labels))
        ):
            errors.append(f"table region labels missing or duplicated: {path.name}")
        if parsed.table_header_count != parsed.scoped_table_header_count:
            errors.append(f"table header scope missing: {path.name}")
        if parsed.unmarked_english_passages:
            errors.append(
                f"English passage missing lang=en: {path.name}: "
                + parsed.unmarked_english_passages[0]
            )
        if parsed.empty_state_is_live:
            errors.append(f"empty state duplicates live search status: {path.name}")
        if "page-lesson" in parsed.classes:
            main_pos = parsed.raw_text.find('class="lesson-main')
            sidebar_pos = parsed.raw_text.find('class="lesson-sidebar')
            if main_pos < 0 or sidebar_pos < 0 or sidebar_pos > main_pos:
                errors.append(f"lesson DOM order invalid: {path.name}")
            if "lesson-provenance" not in parsed.classes:
                errors.append(f"lesson page missing provenance block: {path.name}")
        if "snapshot-note" not in parsed.classes:
            errors.append(f"snapshot provenance missing from footer: {path.name}")
        for tag in ("form", "iframe", "video", "audio", "canvas"):
            if tag in parsed.tags:
                errors.append(f"forbidden <{tag}>: {path.name}")

        for view_box, role, aria, labelledby, describedby, _cls in parsed.inline_svg_nodes:
            title_refs = set(labelledby.split())
            desc_refs = set(describedby.split())
            if (
                not view_box
                or role != "img"
                or aria
                or len(title_refs) != 1
                or len(desc_refs) != 1
                or not title_refs.issubset(parsed.svg_title_ids)
                or not desc_refs.issubset(parsed.svg_desc_ids)
            ):
                errors.append(f"inline svg invalid attrs: {path.name}")
                break

        for line in parsed.raw_text.splitlines():
            if line.strip() in {":::guide", ":::", ":::stretch", ":::zatsudan"}:
                errors.append(f"structure fence remains: {path.name}")
                break

        for line in parsed.main_text.splitlines()[:60]:
            if line.strip().startswith(("verify_required:", "distribution_status:")):
                errors.append(f"frontmatter leaked into <main>: {path.name}")
                break

    markdown = build_report.get("markdown", {})
    if isinstance(markdown, dict):
        expected = markdown.get("expected")
        converted = markdown.get("converted")
        failed = markdown.get("failed")
        if expected != converted:
            errors.append("markdown count mismatch")
        if isinstance(failed, list) and failed:
            errors.append("markdown.failed is not empty")
        checks.append({"name": "markdown", "pass": bool(expected == converted and (not failed))})
    else:
        errors.append("build-report missing markdown block")

    pages = build_report.get("pages", {})
    if isinstance(pages, dict):
        total = pages.get("total")
        if total != len(html_paths):
            errors.append(f"pages.total mismatch: report={total} html={len(html_paths)}")
        if pages.get("updates") != 1:
            errors.append("pages.updates must be exactly one")
        if pages.get("curriculum") != 12:
            errors.append("pages.curriculum must be exactly twelve")
    else:
        errors.append("build-report missing pages.total")

    update_history = build_report.get("update_history", {})
    update_history_ok = True
    expected_update_commits: List[str] = []
    if not isinstance(update_history, dict):
        update_history_ok = False
        errors.append("build-report missing update_history")
    else:
        entries = update_history.get("entries")
        if (
            update_history.get("source") != "canonical_git_history"
            or update_history.get("order") != "first_parent"
            or update_history.get("limit") != 50
            or update_history.get("source_checkout_complete") is not True
            or update_history.get("scope") != "public_display_sources"
            or not isinstance(update_history.get("truncated"), bool)
            or not isinstance(entries, list)
            or not entries
        ):
            update_history_ok = False
            errors.append("build-report update_history contract mismatch")
        else:
            for entry in entries:
                if not isinstance(entry, dict):
                    update_history_ok = False
                    continue
                commit = entry.get("commit")
                date = entry.get("date")
                title = entry.get("title")
                if (
                    not isinstance(commit, str)
                    or not re.fullmatch(r"[0-9a-f]{40}", commit)
                    or not isinstance(date, str)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
                    or not isinstance(title, str)
                    or not title.strip()
                ):
                    update_history_ok = False
                    continue
                expected_update_commits.append(commit)

    home_page = parsed_pages.get(site_root / "index.html")
    curriculum_index_page = parsed_pages.get(site_root / "curriculum/index.html")
    curriculum_grid_ok = True
    legacy_home_emphasis_ok = True
    legacy_home_markers: List[str] = []
    curriculum_report = build_report.get("curriculum_grid", {})
    curriculum_source = resolve_source_root(site_root, args.source, build_report)
    curriculum_snapshot: Dict[str, object] = {}
    if curriculum_source is None:
        curriculum_grid_ok = False
        errors.append("curriculum grid cannot resolve canonical source")
    else:
        try:
            curriculum_snapshot = canonical_curriculum_snapshot(
                curriculum_source,
                Path(__file__).resolve().parent / "curriculum_grid.contract.json",
            )
        except (OSError, ValueError) as exc:
            curriculum_grid_ok = False
            errors.append(f"curriculum grid canonical derivation failed: {exc}")
    if not home_page or not curriculum_index_page:
        curriculum_grid_ok = False
        errors.append("home or curriculum index page is missing for grid validation")
    else:
        if curriculum_snapshot:
            for page_value, current in (
                (home_page.raw_text, PurePosixPath("index.html")),
                (curriculum_index_page.raw_text, PurePosixPath("curriculum/index.html")),
            ):
                grid_errors = curriculum_grid_contract_errors(
                    curriculum_snapshot, curriculum_report, page_value, current
                )
                if grid_errors:
                    curriculum_grid_ok = False
                    for error in grid_errors:
                        if error not in errors:
                            errors.append(error)
            html_by_path = {
                path.relative_to(site_root).as_posix(): parsed.raw_text
                for path, parsed in parsed_pages.items()
            }
            skeleton_errors = curriculum_skeleton_contract_errors(
                curriculum_snapshot, html_by_path
            )
            if skeleton_errors:
                curriculum_grid_ok = False
                errors.extend(skeleton_errors)
            expected_subject_pages = sorted(
                str(subject)
                for family in curriculum_snapshot.get("families", [])
                if isinstance(family, dict)
                for subject in family.get("material_subjects", [])
            )
            actual_subject_pages = sorted(
                path.parent.name for path in (site_root / "subjects").glob("*/index.html")
            )
            if expected_subject_pages != actual_subject_pages:
                curriculum_grid_ok = False
                errors.append("generated subject pages differ from canonical materials")
        if (
            '<section id="learning-grid"' not in home_page.raw_text
            or '<ul class="curriculum-grid" role="list">' not in home_page.raw_text
            or '<a class="primary-action" href="#learning-grid">' not in home_page.raw_text
        ):
            curriculum_grid_ok = False
            errors.append("home learning grid primary navigation or list semantics are missing")
        legacy_markers = (
            "OPEN LEARNING / 中学3年 数学",
            "中3数学の8単元",
            'class="container learning-route"',
            'id="math3-route"',
            'class="learning-grid-tools"',
            "<strong>中3数学で迷っている</strong>",
            ">中3数学の診断を使う<",
            ">中3数学の診断へ<",
        )
        legacy_home_markers = [
            marker for marker in legacy_markers if marker in home_page.raw_text
        ]
        if legacy_home_markers:
            curriculum_grid_ok = False
            legacy_home_emphasis_ok = False
            errors.append("legacy middle-school-math-only home emphasis remains")
    checks.append(
        {
            "name": "content:curriculum_learning_grid",
            "pass": curriculum_grid_ok,
            "entries": len(curriculum_snapshot.get("families", [])),
            "registered_units": len(curriculum_snapshot.get("units", [])),
            "registered_modules": len(curriculum_snapshot.get("modules", [])),
            "packages": curriculum_snapshot.get("total_packages", 0),
            "source": "progress_index_plus_canonical_materials_direct_enumeration",
        }
    )
    checks.append(
        {
            "name": "content:home_legacy_math3_markup_and_dedicated_emphasis_absent",
            "pass": legacy_home_emphasis_ok,
            "matched_markers": legacy_home_markers,
            "scope": "top-page-only legacy DOM and dedicated CTA copy; ordinary update titles are allowed",
        }
    )

    updates_page = parsed_pages.get(site_root / "updates/index.html")
    if not home_page or not updates_page:
        update_history_ok = False
        errors.append("update history page or home page is missing")
    elif expected_update_commits:
        home_commits = re.findall(
            r'data-update-commit="([0-9a-f]{40})"', home_page.raw_text
        )
        page_commits = re.findall(
            r'data-update-commit="([0-9a-f]{40})"', updates_page.raw_text
        )
        if home_commits != expected_update_commits[:3]:
            update_history_ok = False
            errors.append("home update entries do not match the latest three source updates")
        if page_commits != expected_update_commits:
            update_history_ok = False
            errors.append("update history page order does not match build-report")
        if 'class="page-updates"' not in updates_page.raw_text:
            update_history_ok = False
            errors.append("update history page class is missing")
        if 'href="updates/index.html"' not in home_page.raw_text:
            update_history_ok = False
            errors.append("home page link to full update history is missing")
        if '<ol class="updates-list updates-list-compact" role="list">' not in home_page.raw_text:
            update_history_ok = False
            errors.append("home update timeline list semantics are missing")
        if '<ol class="updates-list" role="list">' not in updates_page.raw_text:
            update_history_ok = False
            errors.append("update history timeline list semantics are missing")
        source_for_updates = build_report.get("source")
        source_repository = (
            source_for_updates.get("repository")
            if isinstance(source_for_updates, dict)
            else None
        )
        if not isinstance(source_repository, str) or not source_repository.startswith("https://"):
            update_history_ok = False
            errors.append("update history source repository is missing")
        else:
            for commit in expected_update_commits:
                expected_link = f'{source_repository}/commit/{commit}'
                if expected_link not in updates_page.raw_text:
                    update_history_ok = False
                    errors.append(f"update history fixed commit link missing: {commit}")
                    break
    features_for_updates = build_report.get("features")
    if (
        not isinstance(features_for_updates, dict)
        or features_for_updates.get("update_history_entries") != len(expected_update_commits)
    ):
        update_history_ok = False
        errors.append("features.update_history_entries mismatch")
    checks.append(
        {
            "name": "content:update_history",
            "pass": update_history_ok,
            "entries": len(expected_update_commits),
            "home_entries": min(3, len(expected_update_commits)),
        }
    )

    source_files = build_report.get("source_files", [])
    if not isinstance(source_files, list):
        errors.append("build-report missing source_files")
        source_files = []

    lesson_map: Dict[Path, bool] = {}
    answer_map: Set[Path] = set()
    sha_map: Dict[Path, str] = {}
    source_output_map: Dict[str, Path] = {}
    source_info = build_report.get("source", {})
    source_root: Optional[Path] = None
    built_commit: Optional[str] = None
    source_head_checked = False
    source_commit_matches: Optional[bool] = None
    if isinstance(source_info, dict):
        source_root = resolve_source_root(site_root, args.source, build_report)
        built_commit_value = source_info.get("commit")
        built_commit = built_commit_value if isinstance(built_commit_value, str) else None
        errors.extend(
            reported_source_commit_contract_errors(
                built_commit,
                args.expected_source_sha,
            )
        )
        if source_root and built_commit and (source_root / ".git").exists():
            try:
                def source_git(*git_args: str) -> str:
                    return subprocess.run(
                        ["git", "-C", str(source_root), *git_args],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()

                current_head = source_git("rev-parse", "HEAD")
                current_status = source_git("status", "--porcelain", "--untracked-files=all")
                current_origin = source_git("config", "--get", "remote.origin.url")
                expected_source_head = args.expected_source_sha
                if expected_source_head is None:
                    expected_source_head = source_git(
                        "rev-parse", "refs/remotes/origin/main"
                    )
                errors.extend(
                    source_checkout_contract_errors(
                        source_info,
                        actual_status=current_status,
                        actual_origin=current_origin,
                        actual_head=current_head,
                        expected_head=expected_source_head,
                    )
                )
                source_head_checked = True
                source_commit_matches = built_commit == current_head
                stale = freshness_message(built_commit, current_head)
                if stale:
                    warnings.append(stale)
            except (OSError, subprocess.CalledProcessError) as exc:
                errors.extend(source_checkout_contract_errors(source_info))
                errors.append(f"正本checkoutの来歴検査を実行できませんでした: {exc}")
        else:
            errors.extend(source_checkout_contract_errors(source_info))
    else:
        source_root = resolve_source_root(site_root, args.source, build_report)
        errors.extend(source_checkout_contract_errors(source_info))
    if source_root is None:
        errors.append("正本Git rootを解決できないため、正本SHA・鮮度・検疫・更新履歴を照合できません")
    else:
        source_manifest_errors = source_manifest_contract_errors(
            source_root,
            source_files,
            markdown,
            site_root,
        )
        errors.extend(source_manifest_errors)
        checks.append(
            {
                "name": "source:markdown_manifest_complete",
                "pass": not source_manifest_errors,
                "canonical_files": len(expected_markdown_sources(source_root)),
                "reported_files": len(source_files),
            }
        )
        source_workflow = source_root / ".github/workflows/quarantine.yml"
        workflow_ok = source_workflow.is_file() and sha256_file(source_workflow) == SOURCE_QUARANTINE_WORKFLOW_SHA256
        checks.append(
            {
                "name": "source:quarantine_workflow_sha256",
                "pass": workflow_ok,
                "expected": SOURCE_QUARANTINE_WORKFLOW_SHA256,
                "actual": sha256_file(source_workflow) if source_workflow.is_file() else None,
                "scope": "source workflow file bytes only; source history is not checked here",
            }
        )
        if not workflow_ok:
            errors.append("source quarantine workflow sha256 mismatch")

        source_update_history_ok = False
        expected_public_commits: List[str] = []
        if built_commit and re.fullmatch(r"[0-9a-f]{40}", built_commit):
            try:
                expected_public_commits, expected_truncated, source_history_complete = (
                    canonical_public_update_commits(source_root, built_commit)
                )
                reported_history = build_report.get("update_history")
                reported_entries = (
                    reported_history.get("entries")
                    if isinstance(reported_history, dict)
                    else None
                )
                reported_commits = (
                    [entry.get("commit") for entry in reported_entries if isinstance(entry, dict)]
                    if isinstance(reported_entries, list)
                    else []
                )
                source_update_history_ok = (
                    source_history_complete
                    and reported_commits == expected_public_commits
                    and isinstance(reported_history, dict)
                    and reported_history.get("truncated") == expected_truncated
                )
            except (OSError, subprocess.CalledProcessError, ValueError):
                source_update_history_ok = False
        checks.append(
            {
                "name": "source:update_history_git_order",
                "pass": source_update_history_ok,
                "entries": len(expected_public_commits),
            }
        )
        if not source_update_history_ok:
            errors.append("update history does not match canonical first-parent Git history")
    for item in source_files:
        if not isinstance(item, dict):
            continue
        output = item.get("output")
        if not isinstance(output, str):
            continue
        content_out_path = (site_root / output).resolve()
        kind = str(item.get("kind", ""))
        if kind == "lesson":
            lesson_map[content_out_path] = item.get("answer_target") is not None
        if kind == "answer":
            answer_map.add(content_out_path)
        sha = field(item, ["sha256", "source_sha256", "data_sha256"])
        if sha:
            sha_map[content_out_path] = sha
        source_value = item.get("source")
        if isinstance(source_value, str):
            source_output_map[source_value] = content_out_path
        if source_root and isinstance(source_value, str) and sha:
            source_path = (source_root / source_value).resolve()
            try:
                source_path.relative_to(source_root)
            except ValueError:
                errors.append(f"source path escapes root: {source_value}")
                continue
            if not source_path.is_file():
                errors.append(f"source file missing: {source_value}")
            elif sha256_file(source_path) != sha:
                errors.append(f"source sha256 mismatch: {source_value}")
            else:
                source_files_checked += 1
                parsed_output = parsed_pages.get(content_out_path)
                if parsed_output:
                    source_numbers = numeric_tokens(
                        source_visible_text(source_path.read_text(encoding="utf-8"))
                    )
                    rendered_numbers = numeric_tokens(parsed_output.main_text)
                    missing_numbers = {
                        token: count - rendered_numbers[token]
                        for token, count in source_numbers.items()
                        if count > rendered_numbers[token]
                    }
                    if missing_numbers:
                        errors.append(
                            f"numeric content missing: {source_value}: {missing_numbers}"
                        )
                    else:
                        numeric_documents_checked += 1

    for out_path in sha_map:
        p = parsed_pages.get(out_path)
        if not p:
            continue
        if not p.source_sha256_attr:
            errors.append(f"data-source-sha256 missing: {out_path}")
        elif p.source_sha256_attr != sha_map[out_path]:
            errors.append(f"data-source-sha256 mismatch: {out_path}")

    for out_path, has_answer_link in lesson_map.items():
        p = parsed_pages.get(out_path)
        if not p:
            continue
        if "lesson-nav" not in p.classes:
            errors.append(f"lesson page missing .lesson-nav: {out_path}")
        expected_answer_links = 1 if has_answer_link else 0
        if p.answer_link_count != expected_answer_links:
            errors.append(
                f"lesson answer link count mismatch: {out_path}: "
                f"{p.answer_link_count}/{expected_answer_links}"
            )

    for out_path in answer_map:
        p = parsed_pages.get(out_path)
        if not p:
            continue
        if "answer-page" not in p.classes:
            errors.append(f"answer page missing .answer-page: {out_path}")

    features = build_report.get("features", {})
    if isinstance(features, dict):
        if features.get("tagged_blocks_source") != features.get("tagged_blocks_rendered"):
            errors.append("features tagged_blocks mismatch")
        if features.get("svg_references") != features.get("inline_svg_rendered"):
            errors.append("features svg_references mismatch")
        if features.get("svg_source") != features.get("svg_copied"):
            errors.append("features svg_source mismatch")
        if features.get("svg_guard_self_tests") != 6:
            errors.append("features svg_guard_self_tests missing or incomplete")
        review_conflicts = features.get("review_state_conflicts")
        expected_conflicts: Dict[str, str] = {}
        if source_root:
            try:
                expected_conflicts = expected_review_state_conflicts(source_root)
            except (OSError, UnicodeDecodeError):
                errors.append("canonical review state conflicts could not be derived")
        else:
            errors.append("canonical source is required for review state conflict checks")
        notice_sources = {
            source_value
            for source_value, output_path in source_output_map.items()
            if (parsed_output := parsed_pages.get(output_path))
            and "review-state-conflict" in parsed_output.classes
        }
        conflict_notices_ok = review_state_contract_matches(
            expected_conflicts, review_conflicts, notice_sources
        )
        if not conflict_notices_ok:
            errors.append(
                "review state conflicts do not match canonical source, build-report, and HTML notices"
            )
        checks.append(
            {
                "name": "content:review_state_conflicts",
                "pass": conflict_notices_ok,
                "count": len(expected_conflicts),
            }
        )
        search_path = site_root / "_assets/search-index.json"
        try:
            search_entries = json.loads(search_path.read_text(encoding="utf-8"))
            if not isinstance(search_entries, list):
                raise ValueError("search index is not a list")
            if features.get("search_index_entries") != len(search_entries):
                errors.append("features search_index_entries mismatch")
            for entry in search_entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
                    errors.append("search index entry missing url")
                    continue
                target = (site_root / entry["url"]).resolve()
                if target not in parsed_pages:
                    errors.append(f"search index target missing: {entry['url']}")
                    continue
                headings = entry.get("headings", [])
                if not isinstance(headings, list):
                    errors.append(f"search index headings invalid: {entry['url']}")
                    continue
                for heading in headings:
                    anchor = heading.get("anchor") if isinstance(heading, dict) else None
                    if not isinstance(anchor, str) or anchor not in id_map.get(target, set()):
                        errors.append(f"search index anchor missing: {entry['url']}#{anchor}")
            checks.append({"name": "search_index", "pass": True, "entries": len(search_entries)})
        except Exception as exc:
            errors.append(f"search index invalid: {exc}")

    math3_route = build_report.get("math3_route")
    if isinstance(math3_route, dict):
        route_order = math3_route.get("order")
        route_units = math3_route.get("units")
        route_slugs = (
            [item.get("slug") for item in route_units if isinstance(item, dict)]
            if isinstance(route_units, list)
            else []
        )
        route_ok = (
            isinstance(route_order, list)
            and all(
                isinstance(slug, str)
                and re.fullmatch(r"jhs-math-3-[a-z0-9][a-z0-9-]*", slug)
                for slug in route_order
            )
            and route_order == route_slugs
        )
        learning_slugs = [
            slug
            for slug in route_slugs
            if slug not in {"jhs-math-3-diagnostic", "jhs-math-3-appendix"}
        ]
        route_ok = route_ok and math3_route.get("learning_unit_count") == len(learning_slugs)
        checks.append({"name": "route:math3_canonical_slugs", "pass": route_ok})
        if not route_ok:
            errors.append("math3 route contains a non-unit slug or inconsistent count")
    else:
        errors.append("math3 route report is missing")

    # link checks
    for source_path, parsed in parsed_pages.items():
        for tag, attr, value in parsed.links:
            status, msg, target = validate_link(source_path, tag, attr, value, site_root, id_map)
            if status == "external":
                external_links_checked += 1
            elif status == "internal":
                internal_links_checked += 1
            if status == "error":
                broken_links.append(
                    {
                        "source": str(source_path.relative_to(site_root)),
                        "url": value,
                        "reason": msg,
                        "target": str(target) if target else None,
                    }
                )
                errors.append(f"broken link in {source_path.name}: {value} ({msg})")

    # forbidden strings in html/js
    for parsed in parsed_pages.values():
        lowered = parsed.raw_text.lower()
        for keyword in FORBIDDEN_STRINGS:
            if keyword in lowered:
                errors.append(f"forbidden string in html: {keyword} ({parsed.path.name})")
                break

    for js_path in (path for path in public_files if path.suffix.lower() == ".js"):
        lowered = js_path.read_text(encoding="utf-8").lower()
        for keyword in FORBIDDEN_STRINGS:
            if keyword in lowered:
                errors.append(f"forbidden string in js {js_path.name}: {keyword}")
                break

    theme_js_path = site_root / "_assets/theme.js"
    theme_js_text = (
        theme_js_path.read_text(encoding="utf-8") if theme_js_path.exists() else ""
    )
    other_storage_users = [
        path.relative_to(site_root).as_posix()
        for path in public_files
        if path.suffix.lower() == ".js"
        and path.resolve() != theme_js_path.resolve()
        and "localstorage" in path.read_text(encoding="utf-8", errors="replace").lower()
    ]
    theme_storage_errors = (
        theme_storage_contract_errors(theme_js_text, other_storage_users)
        if theme_js_path.is_file()
        else ["theme script is missing"]
    )
    theme_storage_ok = not theme_storage_errors
    checks.append(
        {
            "name": "privacy:theme_preference_only",
            "pass": theme_storage_ok,
            "storage_key": "manabigrid-theme",
            "other_storage_users": other_storage_users,
            "contract_errors": theme_storage_errors,
        }
    )
    if not theme_storage_ok:
        errors.append("localStorage is not limited to the single theme preference contract")

    site_js_path = site_root / "_assets/site.js"
    site_js_text = site_js_path.read_text(encoding="utf-8") if site_js_path.exists() else ""
    print_details_ok = (
        "beforeprint" in site_js_text
        and "afterprint" in site_js_text
        and "data-progress-disclosure" in site_js_text
    )
    checks.append({"name": "print:progress_disclosures", "pass": print_details_ok})
    if not print_details_ok:
        errors.append("progress disclosures are not prepared for print")

    for asset_path in (
        path for path in public_files if path.suffix.lower() in {".html", ".css", ".js"}
    ):
        if "/Users/" in asset_path.read_text(encoding="utf-8", errors="replace"):
            errors.append(f"local absolute path leaked into public asset: {asset_path.relative_to(site_root)}")

    for svg_path in (path for path in public_files if path.suffix.lower() == ".svg"):
        svg_text = svg_path.read_text(encoding="utf-8", errors="replace")
        if re.search(
            r"<!DOCTYPE\b|<!ENTITY\b|<\?(?!xml\s|xml\?>)|<script\b|<style\b|<foreignObject\b|\son[a-z]+\s*=|@import|url\((?!\s*#)",
            svg_text,
            re.IGNORECASE,
        ):
            errors.append(f"active SVG content is not allowed: {svg_path.relative_to(site_root)}")
        if re.search(
            r"(?:href|xlink:href)\s*=\s*['\"](?:https?:)?//",
            svg_text,
            re.IGNORECASE,
        ):
            errors.append(f"external SVG reference is not allowed: {svg_path.relative_to(site_root)}")
        try:
            svg_root = ET.fromstring(svg_text)
        except ET.ParseError:
            errors.append(f"generated SVG is not valid XML: {svg_path.relative_to(site_root)}")
            continue
        for element in svg_root.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag not in SAFE_SVG_TAGS:
                errors.append(f"unsafe generated SVG element <{tag}>: {svg_path.relative_to(site_root)}")
            for raw_name, raw_value in element.attrib.items():
                name = raw_name.rsplit("}", 1)[-1].lower()
                value = raw_value.strip()
                normalized_value = normalized_css_value(value)
                if name == "style" or name.startswith("on"):
                    errors.append(f"active generated SVG attribute {name}: {svg_path.relative_to(site_root)}")
                if name == "href" and value and not value.startswith("#"):
                    errors.append(f"external generated SVG href: {svg_path.relative_to(site_root)}")
                if re.search(r"@import|url\s*\((?!\s*#)", normalized_value, re.IGNORECASE):
                    errors.append(f"external generated SVG CSS reference: {svg_path.relative_to(site_root)}")

    check_css_rules(site_root, errors, checks)
    validate_public_metadata(site_root, parsed_pages, build_report, errors, checks)
    external_link_appendix = validate_external_link_appendix(
        site_root,
        errors,
        warnings,
        args.external_report,
    )

    result = {
        "status": "failed" if errors else "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "html_pages": len(html_paths),
        "internal_links_checked": internal_links_checked,
        "external_links_checked": external_links_checked,
        "source_files_sha256_checked": source_files_checked,
        "numeric_content_documents_checked": numeric_documents_checked,
        "source_head_checked": source_head_checked,
        "source_commit_matches": source_commit_matches,
        "broken_links": broken_links,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "checks": checks,
        "external_link_report": external_link_appendix,
    }
    report_out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not errors:
        print(
            f"HTML件数: {len(html_paths)}、内部リンク数: {internal_links_checked}、外部リンク数: {external_links_checked}、リンク切れ0"
        )
        return 0
    print(f"リンク切れ: {len(broken_links)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
