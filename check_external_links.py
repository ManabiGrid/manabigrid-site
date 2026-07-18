#!/usr/bin/env python3
"""Record one low-concurrency reachability attempt for each external URL.

Network access is deliberately opt-in.  Without ``--run`` this writes a
not-run report, so ordinary local builds remain network-free.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from public_site import iter_public_files


DEFAULT_TIMEOUT_SECONDS = 12
MAX_WORKERS = 3
BLOCKED_OR_UNKNOWN_HTTP_CODES = {401, 403, 407, 408, 429, 451}


class ExternalUrlCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: set[str] = set()

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for attribute, value in attrs:
            if attribute.lower() not in {"href", "src"} or not value:
                continue
            parsed = urlsplit(value.strip())
            if parsed.scheme.lower() in {"http", "https"}:
                self.urls.add(value.strip())


def site_origin(site_root: Path) -> tuple[str, str] | None:
    config_path = Path(__file__).resolve().parent / "site.config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        parsed = urlsplit(str(config["base_url"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return (parsed.scheme.lower(), parsed.netloc.lower()) if parsed.netloc else None


def collect_external_urls(site_root: Path) -> list[str]:
    own_origin = site_origin(site_root)
    urls: set[str] = set()
    for path in iter_public_files(site_root):
        if path.suffix.lower() != ".html":
            continue
        parser = ExternalUrlCollector()
        parser.feed(path.read_text(encoding="utf-8"))
        for url in parser.urls:
            parsed = urlsplit(url)
            if own_origin == (parsed.scheme.lower(), parsed.netloc.lower()):
                continue
            urls.add(url)
    return sorted(urls)


def _request(url: str, method: str, timeout: int) -> tuple[int, str]:
    request = Request(
        url,
        method=method,
        headers={"User-Agent": "ManabiGridLinkCheck/1.0 (+static-site-preflight)"},
    )
    with urlopen(request, timeout=timeout) as response:  # nosec B310 -- explicit opt-in CLI
        return response.getcode(), response.geturl()


def check_url(url: str, timeout: int) -> dict[str, object]:
    """Classify a single URL; inaccessible services are not hard-broken."""
    try:
        status_code, final_url = _request(url, "HEAD", timeout)
        return {"url": url, "classification": "ok", "status_code": status_code, "final_url": final_url}
    except HTTPError as exc:
        if exc.code == 405:
            try:
                status_code, final_url = _request(url, "GET", timeout)
                return {"url": url, "classification": "ok", "status_code": status_code, "final_url": final_url, "method": "GET"}
            except HTTPError as retry_exc:
                exc = retry_exc
            except (URLError, TimeoutError, OSError) as retry_exc:
                return {"url": url, "classification": "blocked_or_unknown", "detail": str(retry_exc), "method": "GET"}
        classification = "blocked_or_unknown" if exc.code in BLOCKED_OR_UNKNOWN_HTTP_CODES or exc.code >= 500 else "hard_broken"
        return {"url": url, "classification": classification, "status_code": exc.code, "detail": str(exc)}
    except (URLError, TimeoutError, OSError) as exc:
        return {"url": url, "classification": "blocked_or_unknown", "detail": str(exc)}


def make_report(urls: Iterable[str], run: bool, timeout: int) -> dict[str, object]:
    url_list = list(urls)
    if not run:
        results = [{"url": url, "classification": "not_run"} for url in url_list]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            results = list(executor.map(lambda url: check_url(url, timeout), url_list))
    counts = {
        label: sum(result["classification"] == label for result in results)
        for label in ("ok", "hard_broken", "blocked_or_unknown", "not_run")
    }
    return {
        "status": "failed" if counts["hard_broken"] else ("not_run" if not run else "ok"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "network_attempted": run,
        "max_workers": MAX_WORKERS,
        "url_count": len(url_list),
        "summary": counts,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_root", nargs="?", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--run", action="store_true", help="Perform the one-time external HTTP checks.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout は正の秒数にしてください")
    site_root = args.site_root.resolve()
    report = make_report(collect_external_urls(site_root), args.run, args.timeout)
    output = (args.output or site_root / "external-link-report.json").resolve()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "外部URL: "
        f"{report['url_count']}件、hard broken {report['summary']['hard_broken']}件、"
        f"blocked/unknown {report['summary']['blocked_or_unknown']}件、"
        f"network {'実行済み' if args.run else '未実行'}"
    )
    return 1 if report["summary"]["hard_broken"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
