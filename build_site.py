#!/usr/bin/env python3
"""ManabiGrid正本から、依存なしのローカル静的展示サイトを再生成する。"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


SITE_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = SITE_ROOT / "site.config.json"


def load_site_config() -> dict[str, str]:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"site.config.jsonを読めません: {exc}") from exc
    required = {
        "base_url",
        "site_repository",
        "source_repository_url",
        "og_image_source",
        "og_image_output",
    }
    missing = sorted(required - raw.keys())
    if missing or any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in required):
        raise RuntimeError("site.config.jsonの必須値が不足しています: " + ", ".join(missing))
    return {key: str(raw[key]).strip() for key in required}


SITE_CONFIG = load_site_config()


def discover_default_source() -> Path:
    configured = os.environ.get("MANABIGRID_SOURCE_ROOT")
    if configured:
        return Path(configured)
    candidates = [SITE_ROOT.parent / "manabigrid"]
    candidates.extend(sorted(SITE_ROOT.parent.glob("manabigrid*/manabigrid")))
    for candidate in candidates:
        if (candidate / "materials").is_dir() and (candidate / ".git").is_dir():
            return candidate
    return candidates[0]


DEFAULT_SOURCE = discover_default_source()
REPO_URL = SITE_CONFIG["source_repository_url"]
DEFAULT_BASE_URL = SITE_CONFIG["base_url"]
OG_IMAGE_SOURCE = Path(SITE_CONFIG["og_image_source"])
OG_IMAGE_OUTPUT = Path(SITE_CONFIG["og_image_output"])
ISSUE_URL = (
    "https://github.com/ManabiGrid/manabigrid/issues/new"
    "?template=error_report.yml"
)
FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSeKBOQ9EB_DGfkh7Y0LYU83w6ghkPTQfH65BSFjVVWtwEEt9Q/viewform"
)
CC_URL = "https://creativecommons.org/licenses/by/4.0/deed.ja"
TAGS = {"guide", "answer", "zatsudan", "stretch", "plus", "internal"}
TAG_LABEL = {
    "guide": "もう一歩くわしく",
    "answer": "答え・解説",
    "zatsudan": "ちょっとひと息",
    "stretch": "チャレンジ",
    "plus": "発展",
    "internal": "制作メモ",
}
SUBJECTS = {
    "jhs-math-3": ("中学3年 数学", 0),
    "jhs-math-2": ("中学2年 数学", 1),
    "jhs-sci-2": ("中学2年 理科", 2),
    "jhs-eng-1": ("中学1年 英語", 3),
    "jhs-jpn": ("中学 国語", 4),
    "jhs-soc": ("中学 社会", 5),
    "hs-math-i": ("高校 数学I", 6),
}
MATH3_ORDER: list[str] = []
BUILD_CONTEXT: dict[str, str] = {
    "commit": "unknown",
    "commit_date": "unknown",
    "generated_at": "unknown",
    "generated_date": "unknown",
    "base_url": DEFAULT_BASE_URL,
    "og_image_url": urllib.parse.urljoin(DEFAULT_BASE_URL, OG_IMAGE_OUTPUT.as_posix()),
}

MATHML_PROTOTYPE_SOURCE = Path(
    "materials/jhs-math-3/jhs-math-3-similar-figures/lesson_10.md"
)
MATHML_PROTOTYPE_EXPRESSION = r"MN∥BC,\quad MN=\frac{1}{2}BC"


class BuildError(RuntimeError):
    pass


@dataclass
class Doc:
    path: Path
    rel: Path
    output: Path
    kind: str
    title: str
    subject: str | None
    unit: str | None
    sha256: str
    frontmatter: bool
    tags: int
    answer_target: Path | None = None
    headings: list[tuple[int, str, str]] = field(default_factory=list)


@dataclass
class Unit:
    slug: str
    subject: str
    title: str
    docs: list[Doc]
    status: str = "候補ドラフト"
    estimated_time: str | None = None

    @property
    def lessons(self) -> list[Doc]:
        return sorted(
            (d for d in self.docs if d.kind == "lesson"),
            key=lambda d: natural_key(d.path.name),
        )

    @property
    def answers(self) -> list[Doc]:
        return sorted(
            (d for d in self.docs if d.kind == "answer"),
            key=lambda d: natural_key(d.path.name),
        )


@dataclass
class Stats:
    tagged_source: int = 0
    tagged_rendered: int = 0
    svg_references: int = 0
    svg_inlined: int = 0
    mathml_prototypes: int = 0
    repaired_links: list[dict[str, str]] = field(default_factory=list)


def math3_order_from_source(source: Path) -> list[str]:
    """Read the public subject README instead of duplicating its unit order."""
    readme = source / "materials/jhs-math-3/README.md"
    raw = readme.read_text(encoding="utf-8")
    linked = re.findall(r"\]\((jhs-math-3-[^/]+)/README\.md\)", raw)
    unique: list[str] = []
    for slug in linked:
        if slug not in unique:
            unique.append(slug)
    learning = [
        slug
        for slug in unique
        if slug not in {"jhs-math-3-diagnostic", "jhs-math-3-appendix"}
    ]
    if not learning:
        raise BuildError("materials/jhs-math-3/README.md から学習順を取得できません")
    order: list[str] = []
    for slug in ("jhs-math-3-diagnostic", *learning, "jhs-math-3-appendix"):
        if (source / "materials/jhs-math-3" / slug).is_dir():
            order.append(slug)
    return order


def unit_estimated_time(unit_dir: Path, slug: str) -> str | None:
    """Return only time estimates explicitly recorded in the source."""
    if slug.endswith("-diagnostic"):
        diagnostic = unit_dir / "diagnostic.md"
        if diagnostic.exists():
            diagnostic_raw = diagnostic.read_text(encoding="utf-8")
            _, _, metadata = strip_frontmatter(diagnostic_raw)
            value = metadata.get("所要時間目安")
            if value:
                return value
            # Japanese frontmatter keys are intentionally not part of the
            # general metadata parser.  This public display value is therefore
            # read explicitly from the source frontmatter.
            match = re.search(r"^所要時間目安\s*:\s*(.+?)\s*$", diagnostic_raw, re.MULTILINE)
            if match:
                return match.group(1).strip('"\'')
    lesson_map = unit_dir / "lesson_map.md"
    if not lesson_map.exists():
        return None
    raw = lesson_map.read_text(encoding="utf-8")
    match = re.search(r"全\s*([0-9]+(?:\.[0-9]+)?)\s*時間", raw[:4000])
    if match:
        return f"{match.group(1)}時間"
    match = re.search(r"合計\s*[:：]\s*\*\*?([0-9]+(?:\.[0-9]+)?)時間", raw)
    return f"{match.group(1)}時間" if match else None


def natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Generated strings already use LF; omitting ``newline`` keeps compatibility
    # with the system Python 3.9 as well as current Python releases.
    path.write_text(value, encoding="utf-8")


def git(source: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def strip_frontmatter(raw: str) -> tuple[str, bool, dict[str, str]]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return raw, False, {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            metadata: dict[str, str] = {}
            for line in lines[1:i]:
                match = re.match(r"^([A-Za-z_][\w-]*):\s*(.+?)\s*$", line)
                if match:
                    metadata[match.group(1)] = match.group(2).strip("\"'")
            return "\n".join(lines[i + 1 :]).lstrip("\n"), True, metadata
    raise BuildError("frontmatterの終端がありません")


def strip_generated_nav(text: str) -> str:
    text = re.sub(
        r"<!--\s*gen_nav:nav:start.*?<!--\s*gen_nav:nav:end\s*-->",
        "",
        text,
        flags=re.DOTALL,
    )
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def plain(value: str) -> str:
    value = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


def mathml_prototype(expression: str) -> str:
    """Render the one approved static MathML trial without a TeX runtime."""
    if expression != MATHML_PROTOTYPE_EXPRESSION:
        raise BuildError("MathML試作の対象外の式です")
    return (
        '<div class="math-block mathml-prototype" aria-label="数式">'
        '<span class="formula-label">数式・MathML試作</span>'
        '<math display="block" aria-label="MNはBCに平行、MNはBCの2分の1">'
        '<semantics><mrow><mi>MN</mi><mo>∥</mo><mi>BC</mi><mo>,</mo>'
        '<mspace width="1em"></mspace><mi>MN</mi><mo>=</mo>'
        '<mfrac><mn>1</mn><mn>2</mn></mfrac><mi>BC</mi></mrow>'
        '<annotation encoding="application/x-tex">'
        + html.escape(expression)
        + '</annotation></semantics></math></div>'
    )


def title_of(raw: str, fallback: str) -> str:
    body, _, _ = strip_frontmatter(raw)
    match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    return plain(match.group(1)) if match else fallback


def kind_of(path: Path) -> str:
    name = path.name
    if re.fullmatch(r"lesson_\d+\.md", name) or name in {
        "diagnostic.md",
        "student_textbook_print.md",
    }:
        return "lesson"
    if name.startswith("answer_key") or name == "diagnostic_answers.md":
        return "answer"
    if name.startswith("teacher_"):
        return "teacher"
    if name == "PROGRESS_INDEX.md":
        return "progress"
    if name in {"README.md", "lesson_map.md", "diagnostic_map.md", "appendix_map.md"}:
        return "overview"
    return "reference"


def output_for(rel: Path) -> Path:
    if rel == Path("curriculum/PROGRESS_INDEX.md"):
        return Path("progress/index.html")
    return Path("content") / rel.with_suffix(".html")


def collect_docs(source: Path) -> list[Doc]:
    materials = source / "materials"
    paths = sorted(materials.rglob("*.md"))
    paths.append(source / "curriculum/PROGRESS_INDEX.md")
    docs: list[Doc] = []
    for path in paths:
        raw = path.read_text(encoding="utf-8")
        body, fm, _ = strip_frontmatter(raw)
        rel = path.relative_to(source)
        try:
            material_rel = path.relative_to(materials)
        except ValueError:
            material_rel = None
        subject = (
            material_rel.parts[0]
            if material_rel and len(material_rel.parts) >= 2
            else None
        )
        unit = (
            material_rel.parts[1]
            if material_rel and len(material_rel.parts) >= 3
            else None
        )
        tag_count = sum(
            bool(re.fullmatch(r":::[a-z][a-z0-9_-]*\s*", line))
            for line in body.splitlines()
        )
        docs.append(
            Doc(
                path,
                rel,
                output_for(rel),
                kind_of(path),
                title_of(raw, path.stem),
                subject,
                unit,
                sha(path),
                fm,
                tag_count,
            )
        )
    return docs


def register_new_subjects(source: Path, docs: list[Doc]) -> None:
    """Add newly introduced subject folders while keeping known ordering."""
    discovered = sorted({doc.subject for doc in docs if doc.subject})
    for subject in discovered:
        if subject in SUBJECTS:
            continue
        readme = source / "materials" / subject / "README.md"
        label = (
            title_of(readme.read_text(encoding="utf-8"), subject)
            if readme.exists()
            else subject
        )
        SUBJECTS[subject] = (label, len(SUBJECTS))


def progress_statuses(source: Path) -> dict[str, str]:
    text = (source / "curriculum/PROGRESS_INDEX.md").read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[2].startswith("`") and cells[2].endswith("`"):
            found[cells[2].strip("`")] = plain(cells[3])
    return found


def collect_units(source: Path, docs: list[Doc]) -> dict[str, Unit]:
    grouped: dict[str, list[Doc]] = defaultdict(list)
    for doc in docs:
        if doc.unit:
            grouped[doc.unit].append(doc)
    statuses = progress_statuses(source)
    result: dict[str, Unit] = {}
    for slug, items in grouped.items():
        subject = items[0].subject
        assert subject
        readme = source / "materials" / subject / slug / "README.md"
        unit_title = title_of(readme.read_text(encoding="utf-8"), slug)
        unit_title = re.sub(r"\s+[—–-]\s+単元の目次$", "", unit_title)
        result[slug] = Unit(
            slug,
            subject,
            unit_title,
            items,
            statuses.get(slug, "候補ドラフト"),
            unit_estimated_time(readme.parent, slug),
        )
    return result


def quote_path(value: str) -> str:
    if "#" in value:
        path, fragment = value.split("#", 1)
        return urllib.parse.quote(path, safe="/:.") + "#" + urllib.parse.quote(
            fragment, safe="-._~"
        )
    return urllib.parse.quote(value, safe="/:.")


def rel_href(current: Path, target: Path, fragment: str = "") -> str:
    value = posixpath.relpath(target.as_posix(), current.parent.as_posix() or ".")
    if fragment:
        value += "#" + fragment
    return quote_path(value)


class Resolver:
    def __init__(
        self,
        source: Path,
        docs: list[Doc],
        media: dict[Path, Path],
        units: dict[str, Unit],
        stats: Stats,
    ) -> None:
        self.source = source
        self.docs = {d.path.resolve(): d for d in docs}
        self.media = {p.resolve(): out for p, out in media.items()}
        self.units = units
        self.stats = stats

    @staticmethod
    def split_target(raw: str) -> tuple[str, str]:
        raw = raw.strip()
        if raw.startswith("<") and ">" in raw:
            raw = raw[1 : raw.index(">")]
        raw = re.split(r"\s+[\"']", raw, maxsplit=1)[0]
        parts = raw.split("#", 1)
        return urllib.parse.unquote(parts[0]), urllib.parse.unquote(parts[1]) if len(parts) == 2 else ""

    def resolve(self, doc: Doc, raw: str) -> tuple[str, Path | None]:
        target, fragment = self.split_target(raw)
        if target.startswith(("https://", "http://")):
            return target + (("#" + fragment) if fragment else ""), None
        if target == "" and fragment:
            return "#" + quote_path(fragment), doc.path
        if "issues" in target and target.startswith("../"):
            return ISSUE_URL, None
        candidate = (doc.path.parent / target).resolve()
        if candidate in self.docs:
            target_doc = self.docs[candidate]
            return rel_href(doc.output, target_doc.output, fragment), candidate
        if candidate.suffix.lower() == ".svg":
            if candidate in self.media:
                return rel_href(doc.output, self.media[candidate], fragment), candidate
        if candidate.suffix.lower() == ".html":
            md = candidate.with_suffix(".md").resolve()
            if md in self.docs:
                return rel_href(doc.output, self.docs[md].output, fragment), md
            if doc.unit:
                return rel_href(doc.output, Path("units") / doc.unit / "index.html", fragment), None
        if candidate.is_dir():
            try:
                parts = candidate.relative_to(self.source / "materials").parts
            except ValueError:
                parts = ()
            if len(parts) == 1 and parts[0] in SUBJECTS:
                return rel_href(doc.output, Path("subjects") / parts[0] / "index.html", fragment), None
            if len(parts) >= 2 and parts[1] in self.units:
                return rel_href(doc.output, Path("units") / parts[1] / "index.html", fragment), None
        if candidate.exists():
            relative = candidate.relative_to(self.source)
            route = "tree" if candidate.is_dir() else "blob"
            value = (
                f"{REPO_URL}/{route}/{BUILD_CONTEXT['commit']}/"
                f"{urllib.parse.quote(relative.as_posix())}"
            )
            return value + (("#" + urllib.parse.quote(fragment)) if fragment else ""), None
        self.stats.repaired_links.append(
            {"source": doc.rel.as_posix(), "original": raw, "resolved": REPO_URL}
        )
        return REPO_URL, None


class Anchors:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def make(self, value: str) -> str:
        # Keep literal underscores in identifiers such as ``unit_id`` and
        # ``public_core``; the general display-text helper treats underscores
        # as Markdown emphasis markers.
        value = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
        value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
        value = re.sub(r"[`*~]", "", value)
        value = re.sub(r"<[^>]+>", "", value)
        value = unicodedata.normalize("NFKC", html.unescape(value).strip()).casefold()
        # Match GitHub's heading anchors closely: Unicode letters and
        # underscores survive, punctuation is removed, and each space becomes
        # a hyphen (so "A × B" intentionally produces "a--b").
        value = re.sub(r"[^\w\s\-]", "", value)
        value = re.sub(r"\s", "-", value).strip("-") or "section"
        count = self.counts[value]
        self.counts[value] += 1
        return value if count == 0 else f"{value}-{count}"


class Markdown:
    IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
    CODE = re.compile(r"`([^`\n]+)`")

    def __init__(self, doc: Doc, resolver: Resolver, stats: Stats, progress: bool = False) -> None:
        self.doc = doc
        self.resolver = resolver
        self.stats = stats
        self.progress = progress
        self.anchors = Anchors()
        self.headings: list[tuple[int, str, str]] = []
        self.first_h1 = True
        self.svg_serial = 0
        self.table_serial = 0
        self.current_section = ""

    def inline(self, value: str) -> str:
        tokens: list[str] = []

        def hold(rendered: str) -> str:
            key = f"@@MGTOKEN{len(tokens)}@@"
            tokens.append(rendered)
            return key

        value = self.CODE.sub(lambda m: hold(f"<code>{html.escape(m.group(1))}</code>"), value)

        def image_repl(match: re.Match[str]) -> str:
            alt = plain(match.group(1))
            href, path = self.resolver.resolve(self.doc, match.group(2))
            if not path or path.suffix.lower() != ".svg" or not path.exists():
                return hold(f'<a href="{html.escape(href, quote=True)}">{html.escape(alt or "図版")}</a>')
            svg = path.read_text(encoding="utf-8")
            svg = re.sub(r"^\s*<\?xml[^>]*>\s*", "", svg)
            svg = re.sub(r"^\s*<!DOCTYPE[^>]*>\s*", "", svg)

            # SVG fragment ids share the surrounding HTML document's global id
            # namespace.  Prefix each inlined figure so repeated pattern ids
            # such as h45 cannot collide with another figure or heading.
            self.svg_serial += 1
            svg_key = f"{self.doc.rel.as_posix()}:{path}:{self.svg_serial}"
            prefix = "mg-" + hashlib.sha256(svg_key.encode("utf-8")).hexdigest()[:10] + "-"
            id_pattern = re.compile(r"\bid=([\"'])([^\"']+)\1")
            id_map = {old: prefix + old for _, old in id_pattern.findall(svg)}
            svg = id_pattern.sub(
                lambda m: f'id={m.group(1)}{id_map[m.group(2)]}{m.group(1)}', svg
            )
            svg = re.sub(
                r"url\(#([^)]+)\)",
                lambda m: f"url(#{id_map.get(m.group(1), m.group(1))})",
                svg,
            )
            svg = re.sub(
                r"\b((?:xlink:)?href)=([\"'])#([^\"']+)\2",
                lambda m: (
                    f'{m.group(1)}={m.group(2)}#{id_map.get(m.group(3), m.group(3))}{m.group(2)}'
                ),
                svg,
            )
            svg = re.sub(
                r"\b(aria-(?:labelledby|describedby))=([\"'])([^\"']+)\2",
                lambda m: (
                    f'{m.group(1)}={m.group(2)}'
                    + " ".join(id_map.get(token, token) for token in m.group(3).split())
                    + m.group(2)
                ),
                svg,
            )

            def ensure_svg_text_id(
                svg_value: str,
                tag: str,
                fallback_id: str,
                fallback_text: str,
            ) -> tuple[str, str]:
                match = re.search(
                    rf"<{tag}\b([^>]*)>(.*?)</{tag}>",
                    svg_value,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if match:
                    id_match = re.search(r"\bid=[\"']([^\"']+)[\"']", match.group(1))
                    if id_match:
                        return svg_value, id_match.group(1)
                    opening = re.match(rf"<{tag}\b[^>]*>", match.group(0), re.IGNORECASE)
                    assert opening
                    decorated = opening.group(0)[:-1] + f' id="{fallback_id}">'
                    return (
                        svg_value[: match.start()]
                        + decorated
                        + match.group(0)[opening.end() :]
                        + svg_value[match.end() :],
                        fallback_id,
                    )
                opening = re.search(r"<svg\b[^>]*>", svg_value, re.IGNORECASE)
                if not opening:
                    raise BuildError(f"{self.doc.rel}: SVGの開始タグがありません")
                inserted = (
                    f'<{tag} id="{fallback_id}">{html.escape(fallback_text)}</{tag}>'
                )
                return (
                    svg_value[: opening.end()] + inserted + svg_value[opening.end() :],
                    fallback_id,
                )

            fallback_text = alt or "教材の図"
            svg, title_id = ensure_svg_text_id(
                svg, "title", prefix + "title", fallback_text
            )
            svg, desc_id = ensure_svg_text_id(
                svg, "desc", prefix + "desc", fallback_text
            )
            svg = re.sub(
                rf"(<desc\b[^>]*\bid=[\"']{re.escape(desc_id)}[\"'][^>]*>).*?(</desc>)",
                lambda m: m.group(1) + html.escape(fallback_text) + m.group(2),
                svg,
                count=1,
                flags=re.IGNORECASE | re.DOTALL,
            )

            view_box = re.search(r"\bviewBox=[\"']([^\"']+)[\"']", svg, re.IGNORECASE)
            wide = False
            if view_box:
                try:
                    wide = float(view_box.group(1).split()[2]) >= 500
                except (IndexError, ValueError):
                    pass
            figure_class = "lesson-figure figure-wide" if wide else "lesson-figure"
            attrs = (
                f' class="{figure_class}" role="img" focusable="false"'
                f' aria-labelledby="{title_id}" aria-describedby="{desc_id}"'
            )

            def decorate_svg(match: re.Match[str]) -> str:
                opening = re.sub(
                    r"\s+(?:class|role|focusable|aria-label|aria-labelledby|aria-describedby)=([\"']).*?\1",
                    "",
                    match.group(0),
                    flags=re.IGNORECASE,
                )
                return opening[:-1] + attrs + ">"

            svg = re.sub(r"<svg\b[^>]*>", decorate_svg, svg, count=1, flags=re.IGNORECASE)
            self.stats.svg_references += 1
            self.stats.svg_inlined += 1
            caption = f"<figcaption>{html.escape(alt)}</figcaption>" if alt else ""
            figure_shell_class = "svg-figure is-wide" if wide else "svg-figure"
            scroll_attrs = (
                ' tabindex="0" role="region" aria-label="図を横にスクロール"'
                if wide
                else ""
            )
            hint = (
                '<p class="figure-scroll-hint screen-only" aria-hidden="true">'
                "→ よこにスクロール</p>"
                if wide
                else ""
            )
            return hold(
                f'<figure class="{figure_shell_class}"><div class="figure-scroll"{scroll_attrs}>{svg}</div>'
                f"{hint}{caption}"
                f'<a class="figure-source screen-only" href="{href}">図だけを開く</a></figure>'
            )

        value = self.IMAGE.sub(image_repl, value)

        def link_repl(match: re.Match[str]) -> str:
            label = plain(match.group(1))
            href, _ = self.resolver.resolve(self.doc, match.group(2))
            external = href.startswith(("https://", "http://"))
            attrs = (
                ' class="external-link" target="_blank"'
                ' rel="noopener noreferrer external" referrerpolicy="no-referrer"'
                if external
                else ""
            )
            return hold(
                f'<a href="{html.escape(href, quote=True)}"{attrs}>{html.escape(label)}</a>'
            )

        value = self.LINK.sub(link_repl, value)
        value = value.replace("<u>", hold("<u>")).replace("</u>", hold("</u>"))
        rendered = html.escape(value, quote=False)
        rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
        rendered = re.sub(r"~~(.+?)~~", r"<del>\1</del>", rendered)
        rendered = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", rendered)
        rendered = re.sub(r" {2,}\n", "<br>\n", rendered).replace("\n", " ")
        for index, token in enumerate(tokens):
            rendered = rendered.replace(f"@@MGTOKEN{index}@@", token)
        return rendered

    def paragraph(self, value: str) -> str:
        """Render paragraph text, lifting real Markdown images into blocks.

        Twenty-three source figures share a line with an exercise number or
        explanatory text.  A semantic ``figure`` cannot live inside ``p``, so
        keep the surrounding text as paragraphs and place the figure between
        them.  Image-looking examples inside inline code remain ordinary code.
        """
        code_ranges = [(m.start(), m.end()) for m in self.CODE.finditer(value)]
        images = [
            match
            for match in self.IMAGE.finditer(value)
            if not any(start <= match.start() < end for start, end in code_ranges)
        ]
        if not images:
            return f"<p>{self.inline(value)}</p>"
        parts: list[str] = []
        cursor = 0
        for match in images:
            before = value[cursor : match.start()].strip()
            if before:
                parts.append(f"<p>{self.inline(before)}</p>")
            parts.append(self.inline(match.group(0)))
            cursor = match.end()
        after = value[cursor:].strip()
        if after:
            parts.append(f"<p>{self.inline(after)}</p>")
        return "".join(parts)

    @staticmethod
    def list_match(line: str) -> re.Match[str] | None:
        return re.match(r"^\s*([-+*]|\d+[.)])\s+(.+)$", line)

    @staticmethod
    def indent_width(line: str) -> int:
        return len(line) - len(line.lstrip(" \t"))

    def list_block(self, lines: list[str], index: int) -> tuple[str, int]:
        """Render one Markdown list while preserving source item numbers.

        Educational exercises frequently continue an item on an indented next
        line and some answer keys start a list at a number other than 1.  Both
        carry meaning, so they must not be flattened or renumbered.
        """
        first = self.list_match(lines[index])
        if not first:
            raise BuildError(f"{self.doc.rel}: list parser called on non-list line")
        base_indent = self.indent_width(lines[index])
        ordered = first.group(1)[0].isdigit()
        tag = "ol" if ordered else "ul"
        items: list[str] = []
        first_number: int | None = None
        next_display_number: int | None = None

        while index < len(lines):
            match = self.list_match(lines[index])
            if not match or self.indent_width(lines[index]) != base_indent:
                break
            marker = match.group(1)
            if marker[0].isdigit() != ordered:
                break

            number = int(re.match(r"\d+", marker).group()) if ordered else None
            if first_number is None and number is not None:
                first_number = number
                next_display_number = number + 1
                value_attr = ""
            elif number is not None:
                expected = next_display_number if next_display_number is not None else number
                # Markdown authors commonly write every source marker as
                # ``1.`` to request automatic numbering.  Preserve that
                # convention; only force a value for an intentional jump.
                if number in {1, expected}:
                    displayed = expected
                    value_attr = ""
                else:
                    displayed = number
                    value_attr = f' value="{number}"'
                next_display_number = displayed + 1
            else:
                value_attr = ""
            item_lines = [match.group(2)]
            index += 1

            while index < len(lines):
                current = lines[index]
                current_match = self.list_match(current)
                current_indent = self.indent_width(current)
                if current_match and current_indent == base_indent:
                    break
                if not current.strip():
                    lookahead = index + 1
                    while lookahead < len(lines) and not lines[lookahead].strip():
                        lookahead += 1
                    if lookahead >= len(lines):
                        index = lookahead
                        break
                    next_match = self.list_match(lines[lookahead])
                    next_indent = self.indent_width(lines[lookahead])
                    if next_match and next_indent == base_indent:
                        index = lookahead
                        break
                    if next_indent <= base_indent:
                        break
                    item_lines.append("")
                    index += 1
                    continue
                if current_indent <= base_indent:
                    break
                # Remove the parent list's continuation indent while retaining
                # enough indentation for a nested list to be recognized.
                cut = min(current_indent, base_indent + 2)
                item_lines.append(current[cut:])
                index += 1

            items.append(f"<li{value_attr}>{self.blocks(item_lines)}</li>")

        start = f' start="{first_number}"' if first_number is not None else ""
        return f"<{tag}{start}>{''.join(items)}</{tag}>", index

    @staticmethod
    def table_separator(line: str) -> bool:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)

    @staticmethod
    def split_table(line: str) -> list[str]:
        # 正本に5行ある未エスケープの絶対値記号 |a| 等を先に保護する。
        protected: list[str] = []

        def protect(match: re.Match[str]) -> str:
            protected.append(match.group(0))
            return f"@@ABS{len(protected) - 1}@@"

        value = re.sub(r"\|[A-Za-z][A-Za-z0-9′']*\|", protect, line.strip().strip("|"))
        cells: list[str] = []
        buffer: list[str] = []
        escaped = False
        in_code = False
        for char in value:
            if escaped:
                buffer.append(char)
                escaped = False
            elif char == "\\":
                buffer.append(char)
                escaped = True
            elif char == "`":
                buffer.append(char)
                in_code = not in_code
            elif char == "|" and not in_code:
                cells.append("".join(buffer).strip())
                buffer = []
            else:
                buffer.append(char)
        cells.append("".join(buffer).strip())
        for i, cell in enumerate(cells):
            for number, original in enumerate(protected):
                cell = cell.replace(f"@@ABS{number}@@", original)
            cells[i] = cell
        return cells

    def table(self, lines: list[str], index: int) -> tuple[str, int]:
        headers = self.split_table(lines[index])
        plain_headers = [plain(header) for header in headers]
        status_index = plain_headers.index("状態") if "状態" in plain_headers else None
        canonical_progress_table = bool(
            self.progress
            and plain_headers
            and plain_headers[0] == "unitid"
            and status_index is not None
        )
        self.table_serial += 1
        index += 2
        rows: list[list[str]] = []
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            rows.append(self.split_table(lines[index]))
            index += 1
        header_html = "".join(f'<th scope="col">{self.inline(c)}</th>' for c in headers)
        body: list[str] = []
        for row in rows:
            row += [""] * max(0, len(headers) - len(row))
            attrs = ""
            if self.progress:
                attrs = (
                    ' data-search-item data-search="'
                    + html.escape(plain(" ".join(row)).casefold(), quote=True)
                    + '"'
                )
                if status_index is not None and status_index < len(row):
                    attrs += (
                        ' data-status="'
                        + html.escape(plain(row[status_index]), quote=True)
                        + '"'
                    )
                if canonical_progress_table:
                    attrs += " data-progress-canonical-row"
            cells = "".join(f"<td>{self.inline(c)}</td>" for c in row[: len(headers)])
            body.append(f"<tr{attrs}>{cells}</tr>")
        table_name = plain("・".join(headers[:3])) or "データ"
        table_label = f"{table_name}の表（{self.table_serial}）"
        table_html = (
            '<div class="table-wrap" tabindex="0" role="region" aria-label="'
            + html.escape(table_label, quote=True)
            + '">'
            f"<table><thead><tr>{header_html}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>"
        )
        if self.progress and len(rows) > 12:
            disclosure_label = self.current_section or table_name
            table_html = (
                '<details class="progress-table-disclosure" data-progress-disclosure'
                + (" data-progress-canonical" if canonical_progress_table else "")
                + ">"
                f'<summary><span>{html.escape(disclosure_label)}</span>'
                f'<span class="disclosure-count">{len(rows)}行</span></summary>'
                '<p class="table-scroll-hint screen-only" aria-hidden="true">→ 表は横にスクロールできます</p>'
                f"{table_html}</details>"
            )
        return table_html, index

    def container(self, lines: list[str], index: int, tag: str) -> tuple[str, int]:
        inner: list[str] = []
        depth = 1
        index += 1
        while index < len(lines):
            opener = re.fullmatch(r":::([a-z][a-z0-9_-]*)\s*", lines[index])
            if opener:
                depth += 1
            elif re.fullmatch(r":::\s*", lines[index]):
                depth -= 1
                if depth == 0:
                    break
            inner.append(lines[index])
            index += 1
        if depth:
            raise BuildError(f"{self.doc.rel}: ::: {tag} が閉じていません")
        self.stats.tagged_rendered += 1
        if tag == "internal":
            return "<!-- internal block omitted from public display -->", index + 1
        return (
            f'<div class="callout callout-{tag}" role="note" aria-label="{html.escape(TAG_LABEL[tag], quote=True)}">'
            f'<div class="callout-label">{TAG_LABEL[tag]}</div>'
            f"{self.blocks(inner)}</div>",
            index + 1,
        )

    def blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            opener = re.fullmatch(r":::([a-z][a-z0-9_-]*)\s*", line)
            if opener:
                tag = opener.group(1)
                if tag not in TAGS:
                    raise BuildError(f"{self.doc.rel}: 未対応タグ {tag}")
                rendered, i = self.container(lines, i, tag)
                out.append(rendered)
                continue
            if re.fullmatch(r":::\s*", line):
                raise BuildError(f"{self.doc.rel}: 対応しない :::")
            fence = re.match(r"^```\s*([^`\s]*)", line)
            if fence:
                language = fence.group(1) or "text"
                code: list[str] = []
                i += 1
                while i < len(lines) and not re.match(r"^```\s*$", lines[i]):
                    code.append(lines[i])
                    i += 1
                if i == len(lines):
                    raise BuildError(f"{self.doc.rel}: code fence が閉じていません")
                out.append(
                    f'<pre><code class="language-{html.escape(language)}">'
                    f"{html.escape(chr(10).join(code))}</code></pre>"
                )
                i += 1
                continue
            if line.strip().startswith("$$"):
                math_lines = [line]
                if line.strip() == "$$":
                    i += 1
                    while i < len(lines):
                        math_lines.append(lines[i])
                        if lines[i].strip() == "$$":
                            break
                        i += 1
                math_text = "\n".join(math_lines).strip()
                if math_text.startswith("$$") and math_text.endswith("$$"):
                    math_text = math_text[2:-2].strip()
                if (
                    self.doc.rel == MATHML_PROTOTYPE_SOURCE
                    and math_text == MATHML_PROTOTYPE_EXPRESSION
                ):
                    out.append(mathml_prototype(math_text))
                    self.stats.mathml_prototypes += 1
                    i += 1
                    continue
                # The source uses only these two TeX commands.  Render their
                # plain, equivalent Unicode forms so no external math runtime
                # or raw command text is needed.
                math_text = math_text.replace(r"\quad", "\u2003").replace(
                    r"\frac{1}{2}", "½"
                )
                unsupported = re.findall(r"\\[A-Za-z]+", math_text)
                if unsupported:
                    raise BuildError(
                        f"{self.doc.rel}: 未対応の数式コマンド {', '.join(sorted(set(unsupported)))}"
                    )
                out.append(
                    '<div class="math-block" aria-label="数式">'
                    '<span class="formula-label">数式</span><code>'
                    + html.escape(math_text)
                    + "</code></div>"
                )
                i += 1
                continue
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if heading:
                level = len(heading.group(1))
                heading_text = heading.group(2)
                anchor = self.anchors.make(heading_text)
                self.headings.append((level, plain(heading_text), anchor))
                self.current_section = plain(heading_text)
                if level == 1 and self.first_h1:
                    self.first_h1 = False
                    i += 1
                    continue
                level = max(2, level)
                out.append(
                    f'<h{level} id="{html.escape(anchor)}">'
                    f"{self.inline(heading_text)}</h{level}>"
                )
                i += 1
                continue
            if i + 1 < len(lines) and "|" in line and self.table_separator(lines[i + 1]):
                rendered, i = self.table(lines, i)
                out.append(rendered)
                continue
            list_item = self.list_match(line)
            if list_item:
                rendered, i = self.list_block(lines, i)
                out.append(rendered)
                continue
            if line.lstrip().startswith(">"):
                quoted: list[str] = []
                while i < len(lines) and lines[i].lstrip().startswith(">"):
                    quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                quoted_source = "\n".join(quoted)
                quoted_plain = plain(quoted_source)
                # Only classify an explicitly bold opening label/statement.
                # Words such as "公式" or "定理" later in an example, quote,
                # or production note must not promote the whole block.
                opening = re.match(r"^\s*\*\*(.+?)\*\*(?:\s|$)", quoted[0])
                opening_plain = plain(opening.group(1)) if opening else ""
                key_term = opening_plain.startswith("【ことば】")
                explicit_formula = bool(opening and "公式" in opening_plain)
                theorem = bool(opening and "定理" in opening_plain)
                definition = bool(
                    opening
                    and (
                        key_term
                        or "定義" in opening_plain
                        or re.search(r"という[。．]?$", opening_plain)
                    )
                )
                equation = bool(
                    opening
                    and len(opening_plain) <= 220
                    and re.search(r"(?:＝|(?<![<>])=|≒|∝)", opening_plain)
                )
                block_class = (
                    ' class="formula-block"'
                    if equation or definition or explicit_formula or theorem or key_term
                    else ""
                )
                block_label = (
                    '<span class="formula-label">公式</span>'
                    if explicit_formula
                    else '<span class="formula-label">定理</span>'
                    if theorem
                    else '<span class="formula-label">定義</span>'
                    if definition or key_term
                    else '<span class="formula-label">式</span>' if equation else ""
                )
                out.append(
                    f"<blockquote{block_class}>{block_label}{self.blocks(quoted)}</blockquote>"
                )
                continue
            if re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
                out.append("<hr>")
                i += 1
                continue
            # A figure is a block element.  Emitting it directly avoids the
            # invalid ``<p><figure>`` nesting browsers would otherwise repair
            # differently.
            if self.IMAGE.fullmatch(line.strip()):
                out.append(self.inline(line.strip()))
                i += 1
                continue
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i].strip():
                next_line = lines[i]
                if (
                    next_line.startswith(":::")
                    or re.match(r"^(#{1,6})\s+", next_line)
                    or re.match(r"^```", next_line)
                    or next_line.strip().startswith("$$")
                    or self.IMAGE.fullmatch(next_line.strip())
                    or self.list_match(next_line)
                    or next_line.lstrip().startswith(">")
                    or (
                        i + 1 < len(lines)
                        and "|" in next_line
                        and self.table_separator(lines[i + 1])
                    )
                ):
                    break
                paragraph.append(next_line)
                i += 1
            out.append(self.paragraph(chr(10).join(paragraph)))
        return "\n".join(out)

    def render(self, raw: str) -> str:
        body, frontmatter, _ = strip_frontmatter(raw)
        if frontmatter != self.doc.frontmatter:
            raise BuildError(f"{self.doc.rel}: frontmatter検出が不安定です")
        rendered = self.blocks(strip_generated_nav(body).splitlines())
        self.doc.headings = self.headings
        return rendered


def external(url: str, label: str) -> str:
    return (
        f'<a class="external-link" href="{html.escape(url, quote=True)}"'
        ' target="_blank" rel="noopener noreferrer external"'
        ' referrerpolicy="no-referrer">'
        f"{html.escape(label)}<span class=\"sr-only\">"
        "（外部サイト・新しいタブで開きます）</span></a>"
    )


def footer() -> str:
    commit = BUILD_CONTEXT["commit"]
    commit_short = commit[:8] if commit != "unknown" else "不明"
    commit_url = f"{REPO_URL}/tree/{commit}" if commit != "unknown" else REPO_URL
    return f"""
<footer class="site-footer">
  <div class="container footer-grid">
    <section>
      <h2>出典と利用条件</h2>
      <p>出典: {external(REPO_URL, "ManabiGrid（まなびグリッド） " + REPO_URL)} ／ 教材ライセンス: {external(CC_URL, "CC BY 4.0")}</p>
      <p>表示形式のみ変更（Markdown→HTML）。教材本文の意味・数値は変更していません。</p>
      <p class="snapshot-note">この展示は正本コミット {external(commit_url, commit_short)}（{html.escape(BUILD_CONTEXT['generated_date'])}生成）のスナップショットです。展示版の正本はGitHubにあります。</p>
      <p>ManabiGridの展示版です。学校・公的機関の公式教材や公認サイトではありません。</p>
    </section>
    <section>
      <h2>誤りを見つけたら教えてください</h2>
      <p>{external(ISSUE_URL, "GitHubで誤りを報告")} ／ {external(FORM_URL, "おたよりフォーム")}</p>
      <p class="safety-note"><strong>大切:</strong> GitHubに書いた内容はインターネット全体に公開されます。名前・学校名・住所・連絡先・顔写真などを書かないでください。フォームは名前・アカウント不要、匿名で送れます。</p>
    </section>
  </div>
</footer>"""


def breadcrumbs(current: Path, items: list[tuple[str, Path | None]]) -> str:
    rendered: list[str] = []
    for label, target in items:
        if target is None:
            rendered.append(f'<li aria-current="page">{html.escape(label)}</li>')
        else:
            rendered.append(f'<li><a href="{rel_href(current, target)}">{html.escape(label)}</a></li>')
    return (
        '<nav class="breadcrumb container" aria-label="パンくずリスト"><ol>'
        + "".join(rendered)
        + "</ol></nav>"
    )


def public_url(path: Path) -> str:
    relative = path.as_posix()
    if relative == "index.html":
        relative = ""
    elif relative.endswith("/index.html"):
        relative = relative[: -len("index.html")]
    return urllib.parse.urljoin(BUILD_CONTEXT["base_url"], relative)


def public_path_href(path: Path) -> str:
    base_path = urllib.parse.urlsplit(BUILD_CONTEXT["base_url"]).path
    return posixpath.join(base_path.rstrip("/") or "/", path.as_posix()).replace("//", "/")


def page(
    current: Path,
    title: str,
    description: str,
    body: str,
    crumb_items: list[tuple[str, Path | None]],
    page_class: str,
    source_doc: Doc | None = None,
    robots: str = "index, follow",
    canonical: bool = True,
    base_path_links: bool = False,
) -> str:
    source_attrs = (
        f' data-source-sha256="{source_doc.sha256}"'
        f' data-source-file="{html.escape(source_doc.rel.as_posix(), quote=True)}"'
        if source_doc
        else ""
    )
    local_href = (
        public_path_href
        if base_path_links
        else lambda target: rel_href(current, target)
    )
    search_script = (
        f'  <script src="{local_href(Path("_assets/search-index.js"))}" defer></script>\n'
        if page_class == "page-browse"
        else ""
    )
    full_title = f"{title}｜ManabiGrid 展示版"
    canonical_url = public_url(current)
    canonical_tag = (
        f'  <link rel="canonical" href="{html.escape(canonical_url, quote=True)}">\n'
        if canonical
        else ""
    )
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
  <meta name="theme-color" content="#0b1220" media="(prefers-color-scheme: dark)">
  <meta name="description" content="{html.escape(description, quote=True)}">
  <meta name="robots" content="{html.escape(robots, quote=True)}">
  <meta property="og:locale" content="ja_JP">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="ManabiGrid 展示版">
  <meta property="og:title" content="{html.escape(full_title, quote=True)}">
  <meta property="og:description" content="{html.escape(description, quote=True)}">
  <meta property="og:url" content="{html.escape(canonical_url, quote=True)}">
  <meta property="og:image" content="{html.escape(BUILD_CONTEXT['og_image_url'], quote=True)}">
  <meta property="og:image:width" content="2172">
  <meta property="og:image:height" content="724">
  <meta property="og:image:alt" content="まなびグリッドの名称と、学び直しの道筋を表した教材プロジェクトのバナー">
  <meta name="twitter:card" content="summary_large_image">
  <title>{html.escape(full_title)}</title>
{canonical_tag}  <link rel="icon" type="image/svg+xml" href="{local_href(Path("_assets/favicon.svg"))}">
  <link rel="stylesheet" href="{local_href(Path("_assets/site.css"))}">
</head>
<body class="{page_class}"{source_attrs}>
  <a class="skip-link" href="#main-content">本文へ移動</a>
  <header class="site-header">
    <div class="site-nav container">
      <div class="brand"><a class="brand-link" href="{local_href(Path("index.html"))}"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><span>まなびグリッド<small>ManabiGrid</small></span></a><span class="beta-badge">BETA</span></div>
      <nav class="site-links" aria-label="サイト内"><a href="{local_href(Path("browse/index.html"))}">教材をさがす</a><a href="{local_href(Path("progress/index.html"))}">進捗一覧</a></nav>
    </div>
  </header>
  {breadcrumbs(current, crumb_items)}
  <main id="main-content" class="page-shell" tabindex="-1"{source_attrs}>{body}</main>
{footer()}
{search_script}  <script src="{local_href(Path("_assets/site.js"))}" defer></script>
</body>
</html>
"""


def kind_label(kind: str) -> str:
    return {
        "lesson": "レッスン",
        "answer": "解答",
        "teacher": "指導・制作資料",
        "overview": "案内・設計図",
        "reference": "制作資料",
        "progress": "進捗一覧",
    }[kind]


def answer_target(lesson: Doc, unit: Unit) -> Path | None:
    raw = lesson.path.read_text(encoding="utf-8")
    _, _, metadata = strip_frontmatter(raw)
    candidates: list[str] = []
    candidates += re.findall(r"\[解答[^\]]*\]\(([^)]+\.md)\)", raw)
    candidates += re.findall(r"対応解答[：:]\s*`?([^`\s)]+\.md)", raw)
    if metadata.get("answers_file"):
        candidates.append(metadata["answers_file"])
    if lesson.path.name == "student_textbook_print.md":
        candidates.append("answer_key_supplement.md")
    for candidate in candidates:
        path = (lesson.path.parent / candidate.split("#", 1)[0]).resolve()
        for doc in unit.answers:
            if doc.path.resolve() == path:
                return doc.output
    # 正本の明示リンクが正。fallbackは解答が1件しかない特殊パッケージだけ。
    if len(unit.answers) == 1:
        return unit.answers[0].output
    return None


def assign_answers(units: dict[str, Unit]) -> None:
    for unit in units.values():
        for lesson in unit.lessons:
            lesson.answer_target = answer_target(lesson, unit)


def unit_sort(unit: Unit) -> tuple[object, ...]:
    if unit.subject == "jhs-math-3" and unit.slug in MATH3_ORDER:
        return (0, MATH3_ORDER.index(unit.slug))
    return (1, natural_key(unit.slug))


def lessons_nav(current: Path, unit: Unit, doc: Doc) -> str:
    lessons = unit.lessons
    index = lessons.index(doc)
    previous = lessons[index - 1] if index else None
    following = lessons[index + 1] if index + 1 < len(lessons) else None
    previous_html = (
        f'<a class="lesson-nav-prev" href="{rel_href(current, previous.output)}">'
        f"<span>前のレッスン</span>{html.escape(previous.title)}</a>"
        if previous
        else '<span class="lesson-nav-spacer" aria-hidden="true"></span>'
    )
    next_html = (
        f'<a class="lesson-nav-next" href="{rel_href(current, following.output)}">'
        f"<span>次のレッスン</span>{html.escape(following.title)}</a>"
        if following
        else '<span class="lesson-nav-spacer" aria-hidden="true"></span>'
    )
    return (
        '<nav class="lesson-nav" aria-label="レッスン間の移動">'
        + previous_html
        + f'<a class="lesson-nav-unit" href="{rel_href(current, Path("units") / unit.slug / "index.html")}">単元の目次</a>'
        + next_html
        + "</nav>"
    )


def toc(current: Path, headings: list[tuple[int, str, str]]) -> str:
    section_headings = [item for item in headings if item[0] == 2]
    long_page = len(section_headings) >= 7
    useful = section_headings if long_page else [item for item in headings if item[0] >= 2]
    if not useful:
        return ""
    links = "".join(
        f'<li><a data-section-link="{html.escape(anchor, quote=True)}" href="{rel_href(current, current, anchor)}">{html.escape(title)}</a></li>'
        for _, title, anchor in useful[:30]
    )
    long_class = " toc-long" if long_page else ""
    return f'<nav class="toc{long_class}" aria-label="このページの目次"><h2>このページ</h2><ol>{links}</ol></nav>'


def mobile_section_nav(current: Path, headings: list[tuple[int, str, str]]) -> str:
    sections = [item for item in headings if item[0] == 2]
    if len(sections) < 7:
        return ""
    links = "".join(
        f'<li><a data-section-link="{html.escape(anchor, quote=True)}" href="{rel_href(current, current, anchor)}">{html.escape(title)}</a></li>'
        for _, title, anchor in sections[:30]
    )
    return (
        '<details class="mobile-section-nav"><summary>このページの目次</summary>'
        f'<nav aria-label="長いページの節一覧"><ol>{links}</ol></nav></details>'
    )


def lesson_title_markup(title: str) -> str:
    parts = re.split(r"\s*——\s*", title, maxsplit=1)
    if len(parts) == 1:
        return f'<span class="lesson-title-main">{html.escape(title)}</span>'
    return (
        f'<span class="lesson-title-main">{html.escape(parts[0])}</span>'
        f'<span class="lesson-title-sub">{html.escape(parts[1])}</span>'
    )


def lesson_provenance(doc: Doc) -> str:
    commit = BUILD_CONTEXT["commit"]
    source_url = f"{REPO_URL}/blob/{commit}/{urllib.parse.quote(doc.rel.as_posix())}"
    return f"""
<aside class="lesson-provenance" aria-labelledby="lesson-provenance-title">
  <h2 id="lesson-provenance-title">この教材の来歴</h2>
  <p>{external(source_url, "正本Markdownを固定コミットで見る")}</p>
  <p>誤りや分かりにくい所は、{external(ISSUE_URL, "GitHub Issue")}または{external(FORM_URL, "匿名のおたよりフォーム")}で知らせられます。個人情報は書かないでください。</p>
</aside>"""


def link_diagnostic_unit_mentions(
    rendered: str,
    current: Path,
    units: dict[str, Unit],
) -> str:
    """Link unit-name text nodes without touching source Markdown or existing links."""
    replacements: list[tuple[str, str]] = []
    for slug in MATH3_ORDER:
        if slug.endswith(("-diagnostic", "-appendix")) or slug not in units:
            continue
        title = units[slug].title
        match = re.search(r"「(.+?)」", title)
        label = match.group(1) if match else title
        replacements.append(
            (label, rel_href(current, Path("units") / slug / "index.html"))
        )
        # The source diagnostic table also uses these short, unambiguous unit
        # names.  Link them at build time without changing the source wording.
        if slug.endswith("-inscribed-angle"):
            replacements.append(("円周角", rel_href(current, Path("units") / slug / "index.html")))
        elif slug.endswith("-pythagorean-theorem"):
            replacements.append(("三平方", rel_href(current, Path("units") / slug / "index.html")))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    href_by_label = dict(replacements)
    label_pattern = re.compile("|".join(re.escape(label) for label, _ in replacements))

    def link_table(match: re.Match[str]) -> str:
        parts = re.split(r"(<[^>]+>)", match.group(0))
        anchor_depth = 0
        for index, part in enumerate(parts):
            if part.startswith("<"):
                if re.match(r"<a\b", part, re.IGNORECASE):
                    anchor_depth += 1
                elif re.match(r"</a\b", part, re.IGNORECASE):
                    anchor_depth = max(0, anchor_depth - 1)
                continue
            if anchor_depth:
                continue
            part = label_pattern.sub(
                lambda label_match: (
                    f'<a class="unit-inline-link" href="{href_by_label[label_match.group(0)]}">'
                    f'{html.escape(label_match.group(0))}</a>'
                ),
                part,
            )
            parts[index] = part
        return "".join(parts)

    return re.sub(
        r"<table\b.*?</table>",
        link_table,
        rendered,
        flags=re.IGNORECASE | re.DOTALL,
    )


def doc_crumbs(doc: Doc, units: dict[str, Unit]) -> list[tuple[str, Path | None]]:
    items: list[tuple[str, Path | None]] = [("トップ", Path("index.html"))]
    if doc.kind == "progress":
        return items + [("進捗一覧", None)]
    items.append(("教材をさがす", Path("browse/index.html")))
    if doc.subject:
        items.append((SUBJECTS[doc.subject][0], Path("subjects") / doc.subject / "index.html"))
    if doc.unit:
        items.append((units[doc.unit].title, Path("units") / doc.unit / "index.html"))
    items.append((doc.title, None))
    return items


def doc_body(doc: Doc, rendered: str, units: dict[str, Unit]) -> str:
    github_url = f"{REPO_URL}/blob/{BUILD_CONTEXT['commit']}/{urllib.parse.quote(doc.rel.as_posix())}"
    source_h1 = doc.headings[0][2] if doc.headings and doc.headings[0][0] == 1 else "document-title"
    context = kind_label(doc.kind)
    lesson_position = ""
    if doc.unit:
        unit = units[doc.unit]
        context = f"{SUBJECTS[unit.subject][0]} ／ {unit.title}"
        if doc.kind == "lesson":
            lesson_number = unit.lessons.index(doc) + 1
            long_sections = [
                title
                for level, title, _anchor in doc.headings
                if level == 2 and re.match(r"L\d+\b", title)
            ]
            if len(long_sections) >= 2:
                lesson_position = (
                    '<p class="lesson-position" aria-label="ページ内の節数">'
                    f"SECTIONS <strong>{len(long_sections):02}</strong></p>"
                )
            else:
                lesson_position = (
                    '<p class="lesson-position" aria-label="単元内の位置">'
                    f"LESSON <strong>{lesson_number:02}</strong> / {len(unit.lessons):02}</p>"
                )
    header = f"""
<header id="top" class="lesson-header container">
  <p class="lesson-kicker"><span class="doc-kind">{kind_label(doc.kind)}</span><span>{html.escape(context)}</span><span class="status-chip">候補ドラフト</span></p>
  <h1 id="{html.escape(source_h1, quote=True)}">{lesson_title_markup(doc.title)}</h1>
  <details class="lesson-meta"><summary>教材情報と正本</summary><p><code>{html.escape(doc.rel.as_posix())}</code></p><p>{external(github_url, "正本をGitHubで見る")}</p></details>
</header>"""
    answer_link = ""
    if doc.answer_target:
        answer_link = (
            f'<a class="answer-link" href="{rel_href(doc.output, doc.answer_target)}">'
            "<span>自分で考えてから</span><strong>答えを見る</strong></a>"
        )
    notice = ""
    if doc.kind in {"teacher", "reference"}:
        notice = (
            '<div class="container notice" role="note"><strong>制作資料です。</strong>'
            "生徒向けの学習本文ではなく、指導・設計・図版来歴の記録です。</div>"
        )
    article_class = "lesson-body" + (" answer-page" if doc.kind == "answer" else "")
    if doc.kind == "lesson" and doc.unit:
        unit = units[doc.unit]
        return (
            header
            + '<div class="container lesson-layout">'
            + f'<article class="lesson-main {article_class}">{mobile_section_nav(doc.output, doc.headings)}{rendered}{answer_link}{lesson_provenance(doc)}{lessons_nav(doc.output, unit, doc)}</article>'
            + f'<aside class="lesson-sidebar" aria-label="レッスンの現在位置と目次">{lesson_position}{toc(doc.output, doc.headings)}</aside>'
            + "</div>"
        )
    return header + notice + f'<article class="container {article_class}">{rendered}</article>'


def math3_position(slug: str) -> str | None:
    if slug == "jhs-math-3-diagnostic":
        return "00"
    if slug == "jhs-math-3-appendix":
        return "REF"
    learning_order = [
        item
        for item in MATH3_ORDER
        if item not in {"jhs-math-3-diagnostic", "jhs-math-3-appendix"}
    ]
    if slug in learning_order:
        return f"{learning_order.index(slug) + 1:02}"
    return None


def unit_card(
    current: Path,
    unit: Unit,
    featured: bool = False,
    sequence: str | None = None,
) -> str:
    search = f"{unit.title} {unit.slug} {SUBJECTS[unit.subject][0]}".casefold()
    extra = ""
    if unit.slug.endswith("-diagnostic"):
        extra = '<span class="unit-type">診断テスト・試行版</span>'
    elif unit.slug.endswith("-appendix"):
        extra = '<span class="unit-type">巻末資料</span>'
    status = "" if featured else f'<span class="status-chip">{html.escape(unit.status)}</span>'
    unit_id = "" if featured else f'<p class="unit-id">{html.escape(unit.slug)}</p>'
    sequence_html = (
        f'<span class="unit-sequence" aria-hidden="true">{html.escape(sequence)}</span>'
        if sequence
        else ""
    )
    practical_meta = f"{len(unit.lessons)}レッスン"
    if unit.estimated_time:
        practical_meta += f"・目安 {unit.estimated_time}"
    return f"""
<article class="unit-card{' featured' if featured else ''}" data-search-item data-search="{html.escape(search, quote=True)}">
  {sequence_html}<div class="unit-card-body"><div class="unit-card-top">{extra}{status}</div>
  <h3><a href="{rel_href(current, Path("units") / unit.slug / "index.html")}">{html.escape(unit.title)}</a></h3>
  <p>{html.escape(practical_meta)}</p>{unit_id}</div>
</article>"""


def home_body(units: dict[str, Unit]) -> str:
    current = Path("index.html")
    diagnostic_unit = units.get("jhs-math-3-diagnostic")
    diagnostic = (
        diagnostic_unit.lessons[0].output
        if diagnostic_unit and diagnostic_unit.lessons
        else Path("browse/index.html")
    )
    appendix = (
        Path("units/jhs-math-3-appendix/index.html")
        if "jhs-math-3-appendix" in units
        else Path("browse/index.html")
    )
    learning_slugs = [
        slug
        for slug in MATH3_ORDER
        if slug not in {"jhs-math-3-diagnostic", "jhs-math-3-appendix"}
        and slug in units
    ]
    learning_count = len(learning_slugs)
    cards = "".join(
        unit_card(current, units[slug], True, f"{index:02}")
        for index, slug in enumerate(learning_slugs, start=1)
        if slug in units
    )
    return f"""
<section class="home-hero container">
  <div class="hero-copy">
    <p class="eyebrow">OPEN LEARNING / 中学3年 数学</p>
    <h1><span class="hero-line"><span class="hero-phrase">つまずいた場所は、</span></span><span class="hero-line"><span class="hero-emphasis hero-phrase">次のスタート地点</span><span class="hero-phrase">になる。</span></span></h1>
  <p class="hero-lead">わからなくなった場所まで戻り、自分のペースで学び直すオープン教材です。登録も記録もいりません。</p>
    <div class="hero-actions"><a class="primary-action" href="{rel_href(current, diagnostic)}">はじめる場所を見つける</a><a class="text-link" href="#math3-route">{learning_count}単元から選ぶ</a></div>
  </div>
  <aside class="start-panel" aria-labelledby="start-panel-title">
    <p class="panel-code">START / 入口を選ぶ</p><h2 id="start-panel-title">どこからはじめる？</h2>
    <ol class="start-choices">
      <li><span class="choice-number">01</span><div><strong>勉強しに来た</strong><p>現在地がわからなければ診断へ。単元が決まっていれば{learning_count}単元から選べます。</p><a href="{rel_href(current, diagnostic)}">診断テストを開く</a></div></li>
      <li><span class="choice-number">02</span><div><strong>支える・くわしく知る</strong><p>教材の状態、作り方、ライセンスを確認できます。</p><a href="#project-info">保護者・先生・開発者の方へ</a></div></li>
    </ol>
    <p class="start-note"><strong>点数は出ない。始める場所がわかる。</strong><span> 診断は、学び直す位置を探すための試行版です。</span></p>
  </aside>
</section>
<section id="math3-route" class="container learning-route">
  <header class="section-head"><div><p class="eyebrow">PATH 01—{learning_count:02}</p><h2>中3数学の{learning_count}単元</h2></div><p>順番に進んでも、必要な単元だけ選んでもかまいません。</p></header>
  <nav class="route-tools" aria-label="中3数学の補助入口"><a href="{rel_href(current, diagnostic)}">現在地を診断する</a><a href="{rel_href(current, appendix)}">巻末資料を見る</a></nav>
  <div class="unit-route">{cards}</div>
</section>
<section id="project-info" class="container project-info">
  <div><p class="eyebrow">PROJECT</p><h2>保護者・先生・開発者の方へ</h2><p>作りかけを正直に公開し、検証しながら育てているオープン教材です。単元別の正式な人間レビュー済への昇格はこれからで、公式教材ではありません。</p></div>
  <div class="project-actions"><a class="button button-secondary" href="{rel_href(current, Path("progress/index.html"))}">制作の進捗を見る</a><a class="text-link" href="{rel_href(current, Path("about/index.html"))}">このサイトとGitHubの関係</a>{external(REPO_URL, "GitHubでプロジェクトを見る")}</div>
  <p class="project-note">正本はGitHub上のMarkdownとSVGです。この展示版はfrontmatterを隠し、区切り記法を読みやすい枠へ変え、SVGをページ内に表示します。外部CDN、アクセス解析、入力フォーム、個人情報を集める機能はありません。</p>
</section>"""


def subject_units(units: dict[str, Unit], subject: str) -> list[Unit]:
    return sorted((u for u in units.values() if u.subject == subject), key=unit_sort)


def browse_body(units: dict[str, Unit]) -> str:
    current = Path("browse/index.html")
    ordered = sorted(SUBJECTS, key=lambda s: SUBJECTS[s][1])
    subjects_html = "".join(
        f'<article class="subject-card" data-search-item data-search="{html.escape(SUBJECTS[s][0].casefold(), quote=True)}">'
        f'<p class="eyebrow">{len(subject_units(units, s))}パッケージ</p>'
        f'<h3><a href="{rel_href(current, Path("subjects") / s / "index.html")}">{html.escape(SUBJECTS[s][0])}</a></h3>'
        f'<p>レッスン・本文 {sum(len(u.lessons) for u in subject_units(units, s))}ページ</p></article>'
        for s in ordered
    )
    units_html = "".join(unit_card(current, u) for s in ordered for u in subject_units(units, s))
    math3_count = len(
        [slug for slug in MATH3_ORDER if slug not in {"jhs-math-3-diagnostic", "jhs-math-3-appendix"}]
    )
    return f"""
<section class="container unit-hero"><p class="eyebrow">SUBJECT / UNIT / LESSON</p><h1>教材をさがす</h1><p>まず教科を選び、次に単元を選びます。中3数学は{math3_count}単元に加えて、診断テストと巻末資料があります。</p></section>
<section class="container search-panel"><h2>教材の中をさがす</h2><label for="material-search">教科・単元・レッスンのタイトルや見出し</label><input id="material-search" class="search-input" type="search" data-filter-input data-content-search-input autocomplete="off" placeholder="例: 平方根、英語、湿度"><output class="filter-status" data-filter-status data-filter-unit="件" role="status" aria-live="polite">{len(ordered) + len(units)}件を表示</output><div class="content-search-results" data-content-search-results hidden><h3>本文の候補</h3><ol data-content-search-list></ol></div></section>
<section class="container catalog-section"><header class="section-head"><div><p class="eyebrow">SUBJECTS</p><h2>教科から選ぶ</h2></div></header><div class="card-grid subject-grid">{subjects_html}</div></section>
<section class="container catalog-section"><header class="section-head"><div><p class="eyebrow">ALL UNITS</p><h2>すべての単元・モジュール</h2></div></header><div class="card-grid catalog-grid">{units_html}</div><p class="empty-state" data-empty-state hidden>一致する教材がありません。検索語を短くして、もう一度ためしてください。</p></section>"""


def subject_body(subject: str, units: list[Unit], docs: list[Doc]) -> str:
    current = Path("subjects") / subject / "index.html"
    root_doc = next(
        (
            d
            for d in docs
            if d.subject == subject and d.unit is None and d.path.name == "README.md"
        ),
        None,
    )
    source_link = (
        f'<a class="text-link" href="{rel_href(current, root_doc.output)}">正本の教科案内を読む</a>'
        if root_doc
        else ""
    )
    extra = ""
    if subject == "jhs-math-3":
        unit_map = {item.slug: item for item in units}
        ordered_titles: list[str] = []
        for slug in MATH3_ORDER:
            if slug not in unit_map or slug.endswith(("-diagnostic", "-appendix")):
                continue
            title_match = re.search(r"「(.+?)」", unit_map[slug].title)
            ordered_titles.append(title_match.group(1) if title_match else unit_map[slug].title)
        extra = (
            f"<p>おすすめの順番は、{html.escape('、'.join(ordered_titles))}です。"
            "場所がわからないときは診断テストから始められます。</p>"
        )
    cards = "".join(
        unit_card(
            current,
            unit,
            subject == "jhs-math-3",
            math3_position(unit.slug) if subject == "jhs-math-3" else None,
        )
        for unit in units
    )
    return f"""
<section class="container unit-hero">
  <p class="eyebrow">SUBJECT</p><h1>{html.escape(SUBJECTS[subject][0])}</h1>
  <p>{len(units)}パッケージ、レッスン・本文 {sum(len(u.lessons) for u in units)}ページを収録しています。</p>{extra}{source_link}
</section>
<section class="container catalog-section"><header class="section-head"><div><p class="eyebrow">UNITS</p><h2>単元・資料</h2></div></header><div class="card-grid{' unit-route' if subject == 'jhs-math-3' else ' catalog-grid'}">{cards}</div></section>"""


def resource(current: Path, doc: Doc, sequence: int | None = None) -> str:
    number = (
        f'<span class="resource-number" aria-hidden="true">{sequence:02}</span>'
        if sequence is not None
        else ""
    )
    return (
        f'<li>{number}<div><a href="{rel_href(current, doc.output)}">{html.escape(doc.title)}</a>'
        f'<span class="doc-kind">{kind_label(doc.kind)}</span></div></li>'
    )


def unit_body(unit: Unit) -> str:
    current = Path("units") / unit.slug / "index.html"
    special = ""
    if unit.slug.endswith("-diagnostic"):
        special = (
            '<div class="notice" role="note"><strong>試行版です。</strong>'
            "難しさと判定基準は実利用データによる調整前です。結果は目安にしてください。</div>"
        )
    elif unit.slug.endswith("-appendix"):
        special = '<div class="notice" role="note">用語集と図版来歴を確認できる巻末資料です。</div>'
    lessons = "".join(resource(current, d, index) for index, d in enumerate(unit.lessons, start=1))
    answers = "".join(resource(current, d) for d in unit.answers)
    other = "".join(
        resource(current, d) for d in unit.docs if d.kind not in {"lesson", "answer"}
    )
    start_link = ""
    if unit.lessons:
        start_label = "診断を始める" if unit.slug.endswith("-diagnostic") else "最初のレッスンから始める"
        start_link = (
            f'<a class="primary-action unit-start" href="{rel_href(current, unit.lessons[0].output)}">'
            f"{start_label}</a>"
        )
    return f"""
<section class="container unit-hero">
  <p class="eyebrow">{html.escape(SUBJECTS[unit.subject][0])} / UNIT</p>
  <h1>{html.escape(unit.title)}</h1><p>{len(unit.lessons)}レッスン{'・目安 ' + html.escape(unit.estimated_time) if unit.estimated_time else ''}。順番に進めても、必要な場所から始めてもかまいません。</p>{start_link}{special}
  <details class="unit-meta"><summary>教材の状態とID</summary><p><span class="status-chip">{html.escape(unit.status)}</span> <code>{html.escape(unit.slug)}</code></p></details>
</section>
<section class="container resource-list primary-resources">
  <div><p class="eyebrow">LESSONS</p><h2>レッスン</h2><ol>{lessons or "<li>独立した本文ページはありません。</li>"}</ol></div>
</section>
<details class="container resource-list answer-resources"><summary>解答一覧を見る</summary><p>各レッスンを解き終えたあと、レッスン末の「答えを見る」から開けます。</p><ul>{answers or "<li>独立した解答ファイルはありません。</li>"}</ul></details>
<details class="container resource-list production-resources"><summary>案内・指導・制作資料を見る</summary><ul>{other or "<li>案内・制作資料はありません。</li>"}</ul></details>"""


def progress_counts(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for label in (
        "未着手",
        "調査済",
        "ドラフト",
        "QA済",
        "外部レビュー済",
        "人間レビュー済",
        "公開済",
    ):
        match = re.search(
            rf"^\|\s*(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*\|\s*(?:\*\*)?(\d+)(?:\*\*)?",
            text,
            re.MULTILINE,
        )
        result[label] = int(match.group(1)) if match else 0
    return result


def progress_body(doc: Doc, rendered: str, units: dict[str, Unit]) -> str:
    counts = progress_counts(doc.path.read_text(encoding="utf-8"))
    cards = "".join(
        f'<div class="stat-card{" is-zero" if count == 0 else ""}"><span>{label}</span><strong>{count}</strong></div>'
        for label, count in counts.items()
    )
    source_h1 = doc.headings[0][2] if doc.headings and doc.headings[0][0] == 1 else "progress-title"
    row_count = rendered.count("data-search-item")
    included_count = len(units)
    included_external_reviewed = sum(
        unit.status == "外部レビュー済" for unit in units.values()
    )
    registry_external_reviewed = counts.get("外部レビュー済", 0)
    external_query = urllib.parse.urlencode({"status": "外部レビュー済"})
    canonical_progress_anchor = urllib.parse.quote("全単元一覧unit_id-順")
    return f"""
<header id="top" class="container unit-hero">
  <p class="eyebrow">制作状況 / 大人向け</p><h1 id="{html.escape(source_h1, quote=True)}">{html.escape(doc.title)}</h1>
  <p>レジストリから自動生成された、全単元と科目モジュールの現在地です。教材の同梱と、正式な人間レビュー済・公開済の状態は別です。</p>
</header>
<section class="container readable-now" aria-labelledby="readable-now-title"><div><p class="eyebrow">READ NOW</p><h2 id="readable-now-title">いま、このサイトで読める教材</h2><p><strong>{included_count}パッケージを掲載中。</strong>うち{included_external_reviewed}パッケージが外部レビュー済です。正式な人間レビュー済とは別の状態です。</p></div><div class="readable-actions"><a class="button" href="{rel_href(Path('progress/index.html'), Path('browse/index.html'))}">掲載教材を見る</a><a href="?{external_query}#{canonical_progress_anchor}">外部レビュー済{registry_external_reviewed}件の進捗を見る</a></div></section>
<section class="container progress-summary" aria-labelledby="progress-summary-title"><h2 id="progress-summary-title">全体の状態別件数</h2><div class="stat-grid">{cards}</div></section>
<section class="container search-panel"><h2>表の行をしぼりこむ</h2><label for="progress-search">教科・学年・単元名・unit_id・状態</label><input id="progress-search" class="search-input" type="search" data-filter-input autocomplete="off" placeholder="例: 中3 平方根、外部レビュー済"><output class="filter-status" data-filter-status data-filter-unit="行" role="status" aria-live="polite">{row_count}行を表示</output></section>
<p class="container empty-state" data-empty-state hidden>一致する行がありません。検索語を短くして、もう一度ためしてください。</p>
<article class="container lesson-body progress-index">{rendered}</article>
"""


def about_body() -> str:
    commit = BUILD_CONTEXT["commit"]
    commit_short = commit[:8] if commit != "unknown" else "不明"
    commit_url = f"{REPO_URL}/tree/{commit}" if commit != "unknown" else REPO_URL
    return f"""
<section class="container unit-hero about-hero">
  <p class="eyebrow">ABOUT THIS EXHIBITION</p>
  <h1><span class="title-phrase">GitHubを知らなくても、</span><span class="title-phrase">ここで学べます。</span></h1>
  <p>このサイトは、GitHubに保存されたManabiGridの教材原稿を、読みやすい形へ自動変換した展示版です。教材を読むだけなら、GitHubのアカウントも操作も必要ありません。</p>
</section>
<article class="container about-body">
  <section><p class="eyebrow">01 / READ</p><h2>このサイトの仕事</h2><p>教科、単元、レッスンの順に開き、Markdownの記号を意識せずに教材を読むための場所です。登録、学習履歴の保存、アクセス解析はありません。検索語も端末の外へ送りません。</p><p><a class="button" href="../browse/index.html">教材をさがす</a></p></section>
  <section><p class="eyebrow">02 / REPOSITORY</p><h2>リポジトリページは、この順番で見る</h2><ol class="about-steps"><li><strong>まずREADMEを読む。</strong>プロジェクトの目的、いまある教材、注意点がまとまっています。</li><li><strong>フォルダ名を選んで、下へたどる。</strong>教材本文は<code>materials/</code>、進捗と単元一覧は<code>curriculum/</code>にあります。</li><li><strong>教材ファイルを開く。</strong><code>lesson_01.md</code>のようなMarkdownが原稿、<code>assets/</code>のSVGが図版です。</li></ol><p>{external(REPO_URL, 'GitHubでリポジトリを見る')}</p></section>
  <section><p class="eyebrow">03 / NO ACCOUNT</p><h2>アカウントがなくても、閲覧と保存ができる</h2><p>ページを見るだけならサインイン不要です。教材一式を保存したい場合は、リポジトリ上部の<strong>Code</strong>を開き、<strong>Download ZIP</strong>を選びます。ZIPはその時点のファイル一式で、自動更新はされません。</p></section>
  <section><p class="eyebrow">04 / ISSUE</p><h2>Issueで誤りを知らせる3ステップ</h2><ol class="about-steps"><li>{external('https://github.com/signup', 'GitHubアカウントを作る')}か、持っているアカウントでサインインします。</li><li>リポジトリの<strong>Issues</strong>を開き、<strong>New issue</strong>を選びます。</li><li><strong>誤り報告</strong>テンプレートを選び、対象ファイルと疑問点を書きます。</li></ol><p>{external('https://github.com/ManabiGrid/manabigrid/issues/new/choose', 'Issueテンプレートを選ぶ')} ／ {external(ISSUE_URL, '誤り報告を直接開く')}</p><p class="safety-note"><strong>大切:</strong> Issueはインターネット全体に公開されます。名前、学校名、住所、連絡先、顔写真などは書かないでください。アカウントを作りたくない場合は、{external(FORM_URL, '匿名のおたよりフォーム')}を使えます。</p></section>
  <section><p class="eyebrow">05 / WORDS</p><h2>GitHub用語のミニ辞典</h2><dl class="github-glossary"><div><dt>リポジトリ</dt><dd>教材や図版、説明文をひとまとめに保管する場所。</dd></div><div><dt>コミット</dt><dd>「この時点の変更」を保存した記録。展示版は特定のコミットから作られます。</dd></div><div><dt>Issue</dt><dd>誤りや提案を、公開の話題として記録する場所。</dd></div><div><dt>Markdown</dt><dd>見出しや箇条書きを記号で表す、原稿向けのテキスト形式。</dd></div></dl></section>
  <section><p class="eyebrow">06 / SOURCE</p><h2>固定版で来歴を確かめる</h2><p>各レッスン末の「この教材の来歴」から、そのページを生成したMarkdownの固定版を確認できます。現在の展示は{external(commit_url, '正本コミット ' + commit_short)}から、{html.escape(BUILD_CONTEXT['generated_date'])}に生成しました。</p></section>
  <section><p class="eyebrow">07 / UPDATE</p><h2>正本が更新されたら、検査して展示を更新する</h2><p>この展示は正本の更新を定期的に確認し、変化があったときだけ再生成する設計です。リンク、本文、図版、来歴、公開検疫のすべてに通った生成物だけを次の展示候補にします。失敗時は直前の公開版を残します。</p></section>
</article>"""


def search_index_entries(docs: list[Doc], units: dict[str, Unit]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for doc in docs:
        if doc.kind not in {"lesson", "answer", "overview"}:
            continue
        entries.append(
            {
                "title": doc.title,
                "url": doc.output.as_posix(),
                "kind": kind_label(doc.kind),
                "subject": SUBJECTS[doc.subject][0] if doc.subject else "教材案内",
                "unit": units[doc.unit].title if doc.unit else "",
                "headings": [
                    {"title": title, "anchor": anchor}
                    for level, title, anchor in doc.headings
                    if level in {2, 3}
                ],
            }
        )
    return entries


def math_block_locations(docs: list[Doc]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for doc in docs:
        lines = doc.path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if line.strip().startswith("$$"):
                expression = line.strip().strip("$")[:160]
                is_prototype = (
                    doc.rel == MATHML_PROTOTYPE_SOURCE
                    and expression == MATHML_PROTOTYPE_EXPRESSION
                )
                found.append(
                    {
                        "source": doc.rel.as_posix(),
                        "line": line_number,
                        "expression": expression,
                        "rendering": (
                            "mathml-static-prototype"
                            if is_prototype
                            else "unicode-fallback"
                        ),
                    }
                )
    return found


def not_found_body() -> str:
    return f"""
<section class="container not-found" aria-labelledby="not-found-title">
  <p class="eyebrow">404 / LOST COORDINATE</p>
  <h1 id="not-found-title">この場所には、ページがありません。</h1>
  <p>URLが変わったか、入力した場所が少し違うようです。教材が消えたとは限りません。トップか教材一覧から、もう一度たどれます。</p>
  <div class="not-found-actions"><a class="button" href="{public_path_href(Path('index.html'))}">トップへ戻る</a><a href="{public_path_href(Path('browse/index.html'))}">教材をさがす</a></div>
</section>"""


def sitemap_xml(html_paths: Sequence[str]) -> str:
    urls = [
        public_url(Path(path))
        for path in html_paths
        if path != "404.html"
    ]
    body = "\n".join(
        f"  <url><loc>{html.escape(url)}</loc></url>" for url in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + body
        + "\n</urlset>\n"
    )


def robots_text() -> str:
    base_path = urllib.parse.urlsplit(BUILD_CONTEXT["base_url"]).path.rstrip("/")
    allow_path = (base_path or "") + "/"
    return (
        "User-agent: *\n"
        f"Allow: {allow_path}\n"
        f"Sitemap: {urllib.parse.urljoin(BUILD_CONTEXT['base_url'], 'sitemap.xml')}\n"
    )


def clean(site_root: Path) -> None:
    for name in (
        "_assets",
        "_media",
        "about",
        "browse",
        "content",
        "progress",
        "subjects",
        "units",
    ):
        target = site_root / name
        if target.exists():
            if not target.is_dir():
                raise BuildError(f"生成先がディレクトリではありません: {target}")
            shutil.rmtree(target)
    for name in (
        ".nojekyll",
        "index.html",
        "404.html",
        "robots.txt",
        "sitemap.xml",
        "build-report.json",
        "check-report.json",
    ):
        target = site_root / name
        if target.exists():
            if not target.is_file():
                raise BuildError(f"生成先がファイルではありません: {target}")
            target.unlink()


def copy_assets(source: Path, site_root: Path) -> tuple[dict[Path, Path], int]:
    for name in ("site.css", "site.js", "favicon.svg"):
        if not (SITE_ROOT / "static" / name).exists():
            raise BuildError(f"static/{name} がありません")
    (site_root / "_assets").mkdir(parents=True)
    for name in ("site.css", "site.js", "favicon.svg"):
        shutil.copy2(SITE_ROOT / "static" / name, site_root / "_assets" / name)
    og_source = source / OG_IMAGE_SOURCE
    if not og_source.is_file():
        raise BuildError(f"OG画像がありません: {OG_IMAGE_SOURCE}")
    if OG_IMAGE_OUTPUT.is_absolute() or ".." in OG_IMAGE_OUTPUT.parts:
        raise BuildError("og_image_output は出力先内の相対パスにしてください")
    og_target = site_root / OG_IMAGE_OUTPUT
    og_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(og_source, og_target)
    media: dict[Path, Path] = {}
    paths = sorted((source / "materials").rglob("*.svg"))
    for path in paths:
        output = Path("_media") / path.relative_to(source / "materials")
        target = site_root / output
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        media[path.resolve()] = output
    return media, len(paths)


def build(
    source: Path,
    site_root: Path,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, object]:
    global MATH3_ORDER
    source = source.resolve()
    site_root = site_root.resolve()
    if not (source / "materials").is_dir():
        raise BuildError(f"正本が見つかりません: {source}")
    if source == site_root or source in site_root.parents or site_root in source.parents:
        raise BuildError("正本と出力先は別ツリーにしてください")
    parsed_base = urllib.parse.urlsplit(base_url)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        raise BuildError("base URL は https の絶対URLにしてください")
    base_url = base_url.rstrip("/") + "/"
    before = git(source, "status", "--porcelain")
    commit = git(source, "rev-parse", "HEAD")
    commit_date = git(source, "show", "-s", "--format=%cI", "HEAD")
    generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    BUILD_CONTEXT.update(
        {
            "commit": commit,
            "commit_date": commit_date,
            "generated_at": generated_at,
            "generated_date": generated_at[:10],
            "base_url": base_url,
            "og_image_url": urllib.parse.urljoin(base_url, OG_IMAGE_OUTPUT.as_posix()),
        }
    )
    MATH3_ORDER = math3_order_from_source(source)
    clean(site_root)
    media, svg_count = copy_assets(source, site_root)
    docs = collect_docs(source)
    register_new_subjects(source, docs)
    units = collect_units(source, docs)
    assign_answers(units)
    stats = Stats(tagged_source=sum(d.tags for d in docs))
    resolver = Resolver(source, docs, media, units, stats)
    failures: list[dict[str, str]] = []

    for doc in docs:
        try:
            renderer = Markdown(doc, resolver, stats, doc.kind == "progress")
            rendered = renderer.render(doc.path.read_text(encoding="utf-8"))
            if doc.unit == "jhs-math-3-diagnostic":
                rendered = link_diagnostic_unit_mentions(rendered, doc.output, units)
            body = progress_body(doc, rendered, units) if doc.kind == "progress" else doc_body(doc, rendered, units)
            write(
                site_root / doc.output,
                page(
                    doc.output,
                    doc.title,
                    f"ManabiGrid {kind_label(doc.kind)}: {doc.title}",
                    body,
                    doc_crumbs(doc, units),
                    f"page-{doc.kind}",
                    doc,
                ),
            )
        except Exception as exc:
            failures.append({"source": doc.rel.as_posix(), "error": str(exc)})
    if failures:
        raise BuildError("; ".join(f"{f['source']}: {f['error']}" for f in failures[:8]))

    write(
        site_root / "index.html",
        page(
            Path("index.html"),
            "つまずいたところから学び直せる教材",
            "ManabiGridの教材を読みやすく並べた静的展示サイト",
            home_body(units),
            [("トップ", None)],
            "page-home",
        ),
    )
    write(
        site_root / "browse/index.html",
        page(
            Path("browse/index.html"),
            "教材をさがす",
            "教科、単元、レッスンの順に教材を探せます",
            browse_body(units),
            [("トップ", Path("index.html")), ("教材をさがす", None)],
            "page-browse",
        ),
    )
    write(
        site_root / "about/index.html",
        page(
            Path("about/index.html"),
            "このサイトとGitHubの関係",
            "ManabiGrid展示版の読み方、正本GitHub、更新と誤り報告の案内",
            about_body(),
            [("トップ", Path("index.html")), ("このサイトについて", None)],
            "page-about",
        ),
    )
    write(
        site_root / "404.html",
        page(
            Path("404.html"),
            "ページが見つかりません",
            "ManabiGrid展示版でページを見つけられなかったときの案内",
            not_found_body(),
            [("ページが見つかりません", None)],
            "page-not-found",
            robots="noindex, follow",
            canonical=False,
            base_path_links=True,
        ),
    )
    for subject in sorted(SUBJECTS, key=lambda s: SUBJECTS[s][1]):
        output = Path("subjects") / subject / "index.html"
        write(
            site_root / output,
            page(
                output,
                SUBJECTS[subject][0],
                f"ManabiGrid {SUBJECTS[subject][0]}の教材一覧",
                subject_body(subject, subject_units(units, subject), docs),
                [
                    ("トップ", Path("index.html")),
                    ("教材をさがす", Path("browse/index.html")),
                    (SUBJECTS[subject][0], None),
                ],
                "page-subject",
            ),
        )
    for unit in sorted(units.values(), key=lambda u: (SUBJECTS[u.subject][1], unit_sort(u))):
        output = Path("units") / unit.slug / "index.html"
        write(
            site_root / output,
            page(
                output,
                unit.title,
                f"ManabiGrid {unit.title}のレッスン・解答一覧",
                unit_body(unit),
                [
                    ("トップ", Path("index.html")),
                    ("教材をさがす", Path("browse/index.html")),
                    (SUBJECTS[unit.subject][0], Path("subjects") / unit.subject / "index.html"),
                    (unit.title, None),
                ],
                "page-unit",
            ),
        )

    search_entries = search_index_entries(docs, units)
    search_json = json.dumps(search_entries, ensure_ascii=False, indent=2) + "\n"
    write(site_root / "_assets/search-index.json", search_json)
    write(
        site_root / "_assets/search-index.js",
        "window.MANABIGRID_SEARCH_INDEX = "
        + json.dumps(search_entries, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
    )

    write(site_root / ".nojekyll", "")
    html_paths = sorted(p.relative_to(site_root).as_posix() for p in site_root.rglob("*.html"))
    write(site_root / "robots.txt", robots_text())
    write(site_root / "sitemap.xml", sitemap_xml(html_paths))

    after = git(source, "status", "--porcelain")
    if after != before:
        raise BuildError("ビルド前後で正本Git状態が変わりました")
    source_files = [
        {
            "source": d.rel.as_posix(),
            "output": d.output.as_posix(),
            "sha256": d.sha256,
            "kind": d.kind,
            "answer_target": d.answer_target.as_posix() if d.answer_target else None,
            "frontmatter_stripped": d.frontmatter,
        }
        for d in docs
    ]
    report: dict[str, object] = {
        "status": "built",
        "generated_at": generated_at,
        "source": {
            "repository": REPO_URL,
            "commit": commit,
            "commit_date": commit_date,
            "git_status_before": before,
            "git_status_after": after,
        },
        "markdown": {
            "expected": len(docs),
            "converted": len(docs),
            "failed": failures,
            "success_rate_percent": 100.0,
        },
        "pages": {
            "total": len(html_paths),
            "content": len(docs),
            "top": 1,
            "browse": 1,
            "about": 1,
            "not_found": 1,
            "subjects": len(SUBJECTS),
            "units": len(units),
        },
        "features": {
            "frontmatter_stripped": sum(d.frontmatter for d in docs),
            "tagged_blocks_source": stats.tagged_source,
            "tagged_blocks_rendered": stats.tagged_rendered,
            "svg_references": stats.svg_references,
            "inline_svg_rendered": stats.svg_inlined,
            "svg_source": svg_count,
            "svg_copied": len(media),
            "repaired_source_links": stats.repaired_links,
            "search_index_entries": len(search_entries),
            "mathml_static_prototypes": stats.mathml_prototypes,
            "og_image": {
                "source": OG_IMAGE_SOURCE.as_posix(),
                "output": OG_IMAGE_OUTPUT.as_posix(),
                "sha256": sha(site_root / OG_IMAGE_OUTPUT),
            },
            "sitemap_entries": len([path for path in html_paths if path != "404.html"]),
            "progress_disclosures": sum(
                (site_root / path).read_text(encoding="utf-8").count(
                    'class="progress-table-disclosure"'
                )
                for path in html_paths
                if path == "progress/index.html"
            ),
            "display_math_locations": math_block_locations(docs),
        },
        "math3_route": {
            "source": "materials/jhs-math-3/README.md",
            "order": MATH3_ORDER,
            "learning_unit_count": len(
                [
                    slug
                    for slug in MATH3_ORDER
                    if slug not in {"jhs-math-3-diagnostic", "jhs-math-3-appendix"}
                ]
            ),
            "units": [
                {
                    "slug": slug,
                    "title": units[slug].title,
                    "lessons": len(units[slug].lessons),
                    "estimated_time": units[slug].estimated_time,
                }
                for slug in MATH3_ORDER
                if slug in units
            ],
        },
        "publication": {
            "site_repository": SITE_CONFIG["site_repository"],
            "base_url": base_url,
            "deployment_artifact_allowlist": "public_site.py",
        },
        "source_files": source_files,
    }
    write(site_root / "build-report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=SITE_ROOT)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--no-check", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = build(args.source, args.output, args.base_url)
        print(
            f"生成完了: Markdown {report['markdown']['converted']}/"
            f"{report['markdown']['expected']}件、HTML {report['pages']['total']}ページ"
        )
        if not args.no_check:
            subprocess.run(
                [
                    sys.executable,
                    str(args.output.resolve() / "check_site.py"),
                    str(args.output.resolve()),
                    "--source",
                    str(args.source.resolve()),
                ],
                check=True,
            )
        return 0
    except (BuildError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
