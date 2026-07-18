#!/usr/bin/env python3
"""site.config.json の project base path で静的サイトを localhost 配信する。"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent


def project_base_path() -> str:
    config = json.loads((ROOT / "site.config.json").read_text(encoding="utf-8"))
    parsed = urlsplit(str(config["base_url"]))
    if not parsed.path or parsed.path == "/":
        return "/"
    return parsed.path.rstrip("/") + "/"


class PreviewHandler(SimpleHTTPRequestHandler):
    """公開予定のbase pathだけを、ローカルの生成物rootに対応させる。"""

    server_version = "ManabiGridPreview/1.0"
    root = ROOT
    base_path = project_base_path()

    def _site_path(self) -> Path | None:
        requested = unquote(urlsplit(self.path).path)
        if requested == "/" and self.base_path != "/":
            return self.root / "index.html"
        if not requested.startswith(self.base_path):
            return None
        relative = requested[len(self.base_path):]
        parts = PurePosixPath(relative or "index.html").parts
        if any(part in {"", ".", ".."} for part in parts):
            return None
        candidate = self.root.joinpath(*parts)
        if candidate.is_dir():
            candidate /= "index.html"
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    def _send_file(self, path: Path, status: HTTPStatus = HTTPStatus.OK) -> None:
        content_type = self.guess_type(str(path))
        body = path.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self) -> None:
        fallback = self.root / "404.html"
        if fallback.is_file():
            self._send_file(fallback, HTTPStatus.NOT_FOUND)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "404.html がまだ生成されていません")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = self._site_path()
        if path is None or not path.is_file():
            self._not_found()
            return
        self._send_file(path)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("localhost以外へのbindは許可しません")
    server = ThreadingHTTPServer((args.host, args.port), PreviewHandler)
    print(f"http://{args.host}:{args.port}{PreviewHandler.base_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
