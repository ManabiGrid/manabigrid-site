from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit

from public_site import (
    iter_public_files,
    missing_public_entries,
    quarantine_public_artifact,
    quarantine_self_test,
)


ALLOWED_EXTERNAL_HOSTS = {"github.com", "docs.google.com", "creativecommons.org"}
FORBIDDEN_STRINGS = [
    "fetch(",
    "xmlhttprequest",
    "sendbeacon",
    "analytics",
    "tracking",
    "localstorage",
    "sessionstorage",
]
BAD_SCHEMES = {"javascript", "data", "mailto", "tel"}
SOURCE_QUARANTINE_WORKFLOW_SHA256 = "b0dabf30bcfadcd1eff318e54361304feb64fb659d2e494a31b6651b8ca9bcb9"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass
class ParsedHtml:
    path: Path
    doctype_present: bool = False
    html_lang_ja: bool = False
    title: str = ""
    has_viewport: bool = False
    has_main: bool = False
    has_footer: bool = False
    has_skip_link: bool = False
    has_internal_stylesheet: bool = False
    has_internal_icon: bool = False
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


class HtmlCollector(HTMLParser):
    def __init__(self, path: Path):
        super().__init__(convert_charrefs=True)
        self.data = ParsedHtml(path=path)
        self.svg_depth = 0

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
            if attrs_dict.get("scope") == "col":
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
            property_name = attrs_dict.get("property", "").lower()
            content = attrs_dict.get("content", "").strip()
            if name == "robots":
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
        if tag == "title" and not self.svg_depth:
            self.data.in_title = False
        elif tag == "main":
            self.data.in_main = False
        elif tag == "p":
            self.data.in_paragraph = False
        if tag == "svg":
            self.svg_depth = max(0, self.svg_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.data.in_title:
            self.data.title += data
        if self.data.in_main:
            # Keep text-node boundaries so adjacent table/SVG nodes cannot
            # accidentally merge separate numeric tokens during fidelity checks.
            self.data.main_text += " " + data


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
        candidates.append(explicit)
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
        if (resolved / "materials").is_dir():
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
        if target.name not in {"site.js", "search-index.js"} or "_assets" not in target.as_posix():
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
    raw = re.sub(r"^\s*\d+[.)]\s+", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"^\s*[-+*]\s+", "", raw, flags=re.MULTILINE)
    return raw.replace("<u>", "").replace("</u>", "")


def check_css_rules(site_root: Path, errors: List[str], checks: List[Dict[str, object]]) -> None:
    css_files = list(site_root.rglob("*.css"))
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

    html_paths = sorted(site_root.rglob("*.html"))
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
        if not parsed.has_main:
            errors.append(f"main missing: {path.name}")
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
        if parsed.empty_state_is_live:
            errors.append(f"empty state duplicates live search status: {path.name}")
        if "page-lesson" in parsed.classes:
            main_pos = parsed.raw_text.find('class="lesson-main')
            sidebar_pos = parsed.raw_text.find('class="lesson-sidebar')
            if main_pos < 0 or sidebar_pos < 0 or main_pos > sidebar_pos:
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
    else:
        errors.append("build-report missing pages.total")

    source_files = build_report.get("source_files", [])
    if not isinstance(source_files, list):
        source_files = []
        warnings.append("build-report missing source_files")

    lesson_map: Dict[Path, bool] = {}
    answer_map: Set[Path] = set()
    sha_map: Dict[Path, str] = {}
    source_info = build_report.get("source", {})
    source_root: Optional[Path] = None
    source_head_checked = False
    source_commit_matches: Optional[bool] = None
    if isinstance(source_info, dict):
        source_root = resolve_source_root(site_root, args.source, build_report)
        if source_info.get("git_status_before") != source_info.get("git_status_after"):
            errors.append("source git status changed during build")
        built_commit = source_info.get("commit")
        if source_root and isinstance(built_commit, str) and (source_root / ".git").exists():
            try:
                current_head = subprocess.run(
                    ["git", "-C", str(source_root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                source_head_checked = True
                source_commit_matches = built_commit == current_head
                stale = freshness_message(built_commit, current_head)
                if stale:
                    warnings.append(stale)
            except (OSError, subprocess.CalledProcessError) as exc:
                warnings.append(f"正本HEADの鮮度検査を実行できませんでした: {exc}")
    else:
        source_root = resolve_source_root(site_root, args.source, build_report)
    if source_root is None:
        warnings.append("正本rootを解決できないため、正本SHA・鮮度・検疫workflowの照合を省略しました")
    else:
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

    for js_path in site_root.rglob("*.js"):
        lowered = js_path.read_text(encoding="utf-8").lower()
        for keyword in FORBIDDEN_STRINGS:
            if keyword in lowered:
                errors.append(f"forbidden string in js {js_path.name}: {keyword}")
                break

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

    for asset_path in [*site_root.rglob("*.html"), *site_root.rglob("*.css"), *site_root.rglob("*.js")]:
        if "/Users/" in asset_path.read_text(encoding="utf-8", errors="replace"):
            errors.append(f"local absolute path leaked into public asset: {asset_path.relative_to(site_root)}")

    for svg_path in site_root.rglob("*.svg"):
        svg_text = svg_path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"<script\b|\son[a-z]+\s*=", svg_text, re.IGNORECASE):
            errors.append(f"active SVG content is not allowed: {svg_path.relative_to(site_root)}")
        if re.search(
            r"(?:href|xlink:href)\s*=\s*['\"](?:https?:)?//",
            svg_text,
            re.IGNORECASE,
        ):
            errors.append(f"external SVG reference is not allowed: {svg_path.relative_to(site_root)}")

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
