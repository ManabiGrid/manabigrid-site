#!/usr/bin/env python3
"""既存のローカルChromeを使う任意の実描画検査（外部通信・追加依存なし）。"""

from __future__ import annotations

import base64
import argparse
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
REVIEW_DIR = ROOT / "review" / "browser"
MOBILE_VIEWPORT = {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}
DESKTOP_VIEWPORT = {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False}
PAGES = (
    ("top", ROOT / "index.html"),
    (
        "lesson-wide-svg",
        ROOT
        / "content/materials/jhs-sci-2/jhs-sci-2-humidity-calculation/lesson_03.html",
    ),
    ("progress", ROOT / "progress/index.html"),
    ("about", ROOT / "about/index.html"),
    (
        "mathml",
        ROOT / "content/materials/jhs-math-3/jhs-math-3-similar-figures/lesson_10.html",
    ),
)
NOT_FOUND_ROUTE = "browser-check-not-found"


def find_chrome() -> str:
    candidates = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError("ローカルのGoogle Chrome / Chromiumが見つかりません")


class WebSocket:
    """DevTools用の最小WebSocket client（外部パッケージ不使用）。"""

    def __init__(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError(f"ローカル以外のDevTools URLは拒否します: {url}")
        self.socket = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=10)
        self.buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port or 80}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError("DevTools WebSocketのhandshakeが閉じました")
            self.buffer += chunk
        header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"DevTools WebSocketのhandshakeに失敗: {header!r}")

    def close(self) -> None:
        try:
            self._send_frame(b"", 0x8)
        except OSError:
            pass
        self.socket.close()

    def _read_exact(self, length: int) -> bytes:
        while len(self.buffer) < length:
            chunk = self.socket.recv(max(4096, length - len(self.buffer)))
            if not chunk:
                raise RuntimeError("DevTools WebSocketが予期せず閉じました")
            self.buffer += chunk
        value, self.buffer = self.buffer[:length], self.buffer[length:]
        return value

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes(header) + masked)

    def send_json(self, value: dict[str, object]) -> None:
        self._send_frame(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def receive_json(self) -> dict[str, object]:
        fragments = bytearray()
        text_started = False
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("DevTools WebSocketがclose frameを返しました")
            if opcode == 0x9:
                self._send_frame(payload, 0xA)
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                text_started = True
            elif opcode == 0x0 and text_started:
                fragments.extend(payload)
            else:
                continue
            if final:
                return json.loads(fragments.decode("utf-8"))


class DevToolsSocket:
    """Chrome DevTools Protocolをlocalhost WebSocketで扱う。"""

    def __init__(self, chrome: str) -> None:
        args = [
            chrome,
            "--headless=new",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            "--allow-file-access-from-files",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pings",
            "--safebrowsing-disable-auto-update",
            "--incognito",
            "about:blank",
        ]
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.process.stderr is not None
        websocket_url = ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stderr], [], [], 0.25)
            if not ready:
                if self.process.poll() is not None:
                    break
                continue
            line = self.process.stderr.readline()
            match = re.search(r"DevTools listening on (ws://\S+)", line)
            if match:
                websocket_url = match.group(1)
                break
        if not websocket_url:
            self.process.terminate()
            self.process.wait(timeout=3)
            raise RuntimeError("Chrome DevToolsのlocalhost URLを取得できません")
        self.websocket = WebSocket(websocket_url)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.next_id = 1
        self.events: list[dict[str, object]] = []

    def _drain_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for _line in self.process.stderr:
            pass

    def close(self) -> None:
        try:
            self.websocket.close()
        finally:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        call_id = self.next_id
        self.next_id += 1
        request: dict[str, object] = {"id": call_id, "method": method}
        if params:
            request["params"] = params
        if session_id:
            request["sessionId"] = session_id
        self.websocket.send_json(request)

        while True:
            message = self.websocket.receive_json()
            if message.get("id") != call_id:
                self.events.append(message)
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            result = message.get("result", {})
            return result if isinstance(result, dict) else {}


def open_devtools(chrome: str) -> DevToolsSocket:
    """直前のheadless Chrome終了処理と競合した場合に一度だけ再試行する。"""
    failures: list[str] = []
    for attempt in range(2):
        try:
            return DevToolsSocket(chrome)
        except RuntimeError as exc:
            failures.append(str(exc))
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError(" / ".join(failures))


def evaluate(pipe: DevToolsSocket, session: str, expression: str) -> object:
    result = pipe.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        session,
    )
    remote = result.get("result", {})
    if isinstance(remote, dict) and "value" in remote:
        return remote["value"]
    description = remote.get("description") if isinstance(remote, dict) else None
    raise RuntimeError(f"JavaScript評価値を取得できません: {description}")


METRICS_SCRIPT = r"""
(() => {
  const root = document.documentElement;
  const body = document.body;
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const focusable = [...document.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]'
  )].filter((element) => visible(element) && element.tabIndex >= 0).map((element) => ({
    tag: element.tagName.toLowerCase(),
    class: element.className || '',
    href: element.getAttribute('href') || '',
    label: (element.textContent || element.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 80),
    tabindex: element.tabIndex,
  }));
  const inspect = (selector) => {
    const element = document.querySelector(selector);
    if (!element) return {present: false, visible: false};
    const rect = element.getBoundingClientRect();
    return {
      present: true,
      visible: visible(element),
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      text: (element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120),
    };
  };
  const localScrollers = [...document.querySelectorAll('.figure-scroll, .table-wrap, .math-block, pre')]
    .filter(visible)
    .map((element) => ({
      class: element.className || element.tagName.toLowerCase(),
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      overflowX: getComputedStyle(element).overflowX,
    }));
  const viewportWidth = root.clientWidth;
  const pageWidth = Math.max(root.scrollWidth, body ? body.scrollWidth : 0);
  return {
    title: document.title,
    readyState: document.readyState,
    innerWidth,
    innerHeight,
    scrollX,
    scrollY,
    viewportWidth,
    pageWidth,
    pageOverflow: pageWidth > viewportWidth + 1,
    focusableCount: focusable.length,
    firstFocusable: focusable[0] || null,
    header: inspect('.site-header'),
    brand: inspect('.brand-link'),
    navigation: inspect('.site-links'),
    firstUnitCard: inspect('.unit-route .unit-card'),
    heroPhrases: [...document.querySelectorAll('.hero-phrase')].map((element) => {
      const rect = element.getBoundingClientRect();
      return { text: (element.textContent || '').trim(), width: rect.width, top: rect.top, bottom: rect.bottom, rectCount: element.getClientRects().length };
    }),
    readableNow: inspect('.readable-now'),
    readableAction: inspect('.readable-actions .button'),
    notFoundTitle: inspect('#not-found-title'),
    aboutSteps: document.querySelectorAll('.about-steps li').length,
    aboutTitlePhrases: [...document.querySelectorAll('.about-hero .title-phrase')].map((element) => {
      const rect = element.getBoundingClientRect();
      return { text: (element.textContent || '').trim(), width: rect.width, rectCount: element.getClientRects().length };
    }),
    glossaryTerms: document.querySelectorAll('.github-glossary dt').length,
    glossaryDefinitions: document.querySelectorAll('.github-glossary dd').length,
    mathml: {
      present: Boolean(document.querySelector('math')),
      staticRuntimeFree: !document.querySelector('script[src*="mathjax" i], script[src*="katex" i]'),
    },
    navigationResponseStatus: performance.getEntriesByType('navigation')[0]?.responseStatus || 0,
    figureScroller: inspect('.figure-scroll'),
    progressDisclosureCount: document.querySelectorAll('[data-progress-disclosure]').length,
    progressOpenDisclosureCount: document.querySelectorAll('[data-progress-disclosure][open]').length,
    localScrollers,
  };
})()
"""

APPEARANCE_SCRIPT = r"""
(() => {
  const color = (selector, property) => {
    const element = document.querySelector(selector);
    return element ? getComputedStyle(element)[property] : '';
  };
  return {
    dark: matchMedia('(prefers-color-scheme: dark)').matches,
    print: matchMedia('print').matches,
    rootBackground: color('html', 'backgroundColor'),
    bodyBackground: color('body', 'backgroundColor'),
    figureBackground: color('.svg-figure', 'backgroundColor'),
    figureColor: color('.svg-figure', 'color'),
    primaryButtonBackground: color('.button, .primary-action', 'backgroundColor'),
    primaryButtonColor: color('.button, .primary-action', 'color'),
  };
})()
"""


def wait_until_complete(pipe: DevToolsSocket, session: str, expected_url: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = evaluate(
            pipe,
            session,
            "({href: location.href, readyState: document.readyState})",
        )
        if (
            isinstance(state, dict)
            and state.get("href") == expected_url
            and state.get("readyState") == "complete"
        ):
            return
        time.sleep(0.05)
    raise RuntimeError("ページ読み込みが10秒以内に完了しませんでした")


def settle_first_paint(pipe: DevToolsSocket, session: str) -> None:
    evaluate(
        pipe,
        session,
        "(async () => { if (document.fonts) await document.fonts.ready; scrollTo(0, 0); await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 50)))); return true; })()",
    )


def event_errors(events: list[dict[str, object]], start: int, session: str) -> list[str]:
    errors: list[str] = []
    for event in events[start:]:
        if event.get("sessionId") != session:
            continue
        method = event.get("method")
        params = event.get("params", {})
        if not isinstance(params, dict):
            continue
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            errors.append(f"JavaScript例外: {details}")
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if isinstance(entry, dict) and entry.get("level") == "error":
                errors.append(f"console error: {entry.get('text', '')}")
    return errors


def set_media(
    pipe: DevToolsSocket,
    session: str,
    media: str,
    color_scheme: str | None = None,
) -> None:
    params: dict[str, object] = {"media": media}
    if color_scheme:
        params["features"] = [
            {"name": "prefers-color-scheme", "value": color_scheme}
        ]
    pipe.call("Emulation.setEmulatedMedia", params, session)


def is_white(value: object) -> bool:
    return str(value).replace(" ", "") in {"rgb(255,255,255)", "rgba(255,255,255,1)"}


def contrast_ratio(foreground: object, background: object) -> float:
    """Return the WCAG contrast ratio for opaque computed rgb()/rgba() colors."""

    def luminance(value: object) -> float:
        channels = re.fullmatch(
            r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(?:1(?:\.0+)?))?\s*\)",
            str(value),
        )
        if not channels:
            raise ValueError(f"不透明なRGB色ではありません: {value!r}")
        rgb = [int(channel) / 255 for channel in channels.groups()]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in rgb
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    foreground_luminance = luminance(foreground)
    background_luminance = luminance(background)
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--dark", action="store_true", help="OSダーク配色をエミュレートして撮影")
    parser.add_argument(
        "--base-url",
        help="localhostで配信中のサイトURL（例: http://127.0.0.1:8765）",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/") if args.base_url else None
    if base_url:
        parsed_base = urlsplit(base_url)
        if parsed_base.scheme not in {"http", "https"} or parsed_base.hostname not in {
            "127.0.0.1",
            "localhost",
        }:
            print("--base-url はlocalhostのHTTP(S)だけを指定できます", file=sys.stderr)
            return 2
    mode = "desktop" if args.desktop else "mobile"
    color_scheme = "dark" if args.dark else "light"
    viewport = DESKTOP_VIEWPORT if mode == "desktop" else MOBILE_VIEWPORT
    chrome = find_chrome()
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "status": "ok",
        "chrome": chrome,
        "mode": mode,
        "color_scheme": color_scheme,
        "base_url": base_url,
        "viewport_css_pixels": viewport,
        "pages": [],
        "errors": [],
    }
    errors: list[str] = []
    pipe: DevToolsSocket | None = None
    try:
        pipe = open_devtools(chrome)
        pages: list[dict[str, object]] = []
        page_specs = list(PAGES)
        if base_url:
            page_specs.append(("not-found", ROOT / "404.html"))
        for label, path in page_specs:
            if label == "not-found":
                page_url = f"{base_url}/{NOT_FOUND_ROUTE}"
            else:
                page_url = (
                    f"{base_url}/{path.relative_to(ROOT).as_posix()}"
                    if base_url
                    else path.as_uri()
                )
            target = pipe.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            session = pipe.call(
                "Target.attachToTarget", {"targetId": target, "flatten": True}
            )["sessionId"]
            assert isinstance(session, str)
            pipe.call("Page.enable", session_id=session)
            pipe.call("Runtime.enable", session_id=session)
            pipe.call("Log.enable", session_id=session)
            set_media(pipe, session, "screen", color_scheme)
            pipe.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    **viewport,
                    "screenWidth": viewport["width"],
                    "screenHeight": viewport["height"],
                    "positionX": 0,
                    "positionY": 0,
                },
                session,
            )
            event_start = len(pipe.events)
            navigation = pipe.call("Page.navigate", {"url": page_url}, session)
            if navigation.get("errorText"):
                errors.append(f"{label}: 読み込み失敗 {navigation['errorText']}")
                continue
            wait_until_complete(pipe, session, page_url)
            pipe.call("Page.bringToFront", session_id=session)
            settle_first_paint(pipe, session)
            metrics = evaluate(pipe, session, METRICS_SCRIPT)
            if not isinstance(metrics, dict):
                errors.append(f"{label}: 描画指標を取得できません")
                continue
            if not viewport["mobile"]:
                evaluate(
                    pipe,
                    session,
                    "new Promise((resolve) => { const header = document.querySelector('.site-header'); if (header) header.style.position = 'static'; requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))); })",
                )
            screenshot_origin_y = 0.0
            if label == "lesson-wide-svg":
                figure_scroller = metrics.get("figureScroller")
                if isinstance(figure_scroller, dict) and figure_scroller.get("present"):
                    screenshot_origin_y = max(
                        0.0, float(figure_scroller.get("top", 0.0)) - 120.0
                    )
            screenshot = pipe.call(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": True,
                    "clip": {
                        "x": 0,
                        "y": screenshot_origin_y,
                        "width": viewport["width"],
                        "height": viewport["height"],
                        "scale": 1,
                    },
                },
                session,
            )
            color_suffix = "-dark" if args.dark else ""
            screenshot_path = REVIEW_DIR / f"{label}-{viewport['width']}x{viewport['height']}{color_suffix}.png"
            screenshot_path.write_bytes(base64.b64decode(str(screenshot["data"])))

            page_errors: list[str] = []
            if metrics.get("innerWidth") != viewport["width"]:
                page_errors.append(
                    f"CSS viewportが{viewport['width']}pxではありません: {metrics.get('innerWidth')}px"
                )
            if metrics.get("pageOverflow"):
                page_errors.append(
                    f"ページ全体が横にはみ出しています: {metrics.get('pageWidth')}px"
                )
            if metrics.get("scrollX") != 0 or metrics.get("scrollY") != 0:
                page_errors.append(
                    f"キャプチャ開始位置が左上ではありません: x={metrics.get('scrollX')}, y={metrics.get('scrollY')}"
                )
            header = metrics.get("header")
            brand = metrics.get("brand")
            navigation_metrics = metrics.get("navigation")
            if not isinstance(header, dict) or not header.get("visible") or header.get("top") != 0:
                page_errors.append("共通ヘッダーがviewport上端で可視になっていません")
            if (
                not isinstance(brand, dict)
                or not brand.get("visible")
                or "まなびグリッド" not in str(brand.get("text", ""))
            ):
                page_errors.append("ブランド名が共通ヘッダー内で可視になっていません")
            if (
                not isinstance(navigation_metrics, dict)
                or not navigation_metrics.get("visible")
                or "教材をさがす" not in str(navigation_metrics.get("text", ""))
                or "進捗一覧" not in str(navigation_metrics.get("text", ""))
            ):
                page_errors.append("共通サイトナビが可視になっていません")
            first = metrics.get("firstFocusable")
            if not isinstance(first, dict) or "skip-link" not in str(first.get("class", "")).split():
                page_errors.append("最初のフォーカス対象がskip linkではありません")
            if label == "lesson-wide-svg" and viewport["mobile"]:
                scrollers = metrics.get("localScrollers", [])
                wide_scroller = any(
                    isinstance(item, dict)
                    and "figure-scroll" in str(item.get("class", "")).split()
                    and int(item.get("scrollWidth", 0)) > int(item.get("clientWidth", 0))
                    and item.get("overflowX") == "auto"
                    for item in scrollers
                )
                if not wide_scroller:
                    page_errors.append("wide SVGが局所横スクロールになっていません")
            if label == "top":
                first_unit = metrics.get("firstUnitCard")
                if (
                    not isinstance(first_unit, dict)
                    or not first_unit.get("present")
                    or float(first_unit.get("top", viewport["height"] + 1))
                    >= viewport["height"]
                ):
                    page_errors.append("第一画面に先頭単元カードが覗いていません")
                hero_phrases = metrics.get("heroPhrases", [])
                if not hero_phrases or any(
                    not isinstance(item, dict)
                    or int(item.get("rectCount", 0)) != 1
                    or float(item.get("width", viewport["width"] + 1)) > viewport["width"]
                    for item in hero_phrases
                ):
                    page_errors.append("ヒーローの句が語中改行なしで収まっていません")
                elif (
                    len(hero_phrases) != 3
                    or abs(float(hero_phrases[0].get("top", 0)) - float(hero_phrases[1].get("top", 0))) < 1
                    or abs(float(hero_phrases[1].get("top", 0)) - float(hero_phrases[2].get("top", 0))) > 1
                ):
                    page_errors.append("ヒーロー見出しが意図した文節2行になっていません")
            if label == "progress":
                if metrics.get("progressDisclosureCount") != 12:
                    page_errors.append("進捗の大きな表が12区分の折りたたみになっていません")
                if metrics.get("progressOpenDisclosureCount") != 0:
                    page_errors.append("進捗の表が初期状態で閉じていません")
                readable_now = metrics.get("readableNow")
                readable_action = metrics.get("readableAction")
                if (
                    not isinstance(readable_now, dict)
                    or not readable_now.get("visible")
                    or not isinstance(readable_action, dict)
                    or not readable_action.get("visible")
                ):
                    page_errors.append("進捗ページのCTAが可視になっていません")
                query_url = f"{page_url}?status=%E5%A4%96%E9%83%A8%E3%83%AC%E3%83%93%E3%83%A5%E3%83%BC%E6%B8%88"
                pipe.call("Page.navigate", {"url": query_url}, session)
                wait_until_complete(pipe, session, query_url)
                settle_first_paint(pipe, session)
                query_result = evaluate(
                    pipe,
                    session,
                    "({value: document.querySelector('[data-filter-input]')?.value || '', open: document.querySelectorAll('[data-progress-disclosure][open]').length, visible: document.querySelectorAll('[data-search-item]:not([hidden])').length})",
                )
                if not isinstance(query_result, dict) or query_result.get("value") != "外部レビュー済":
                    page_errors.append("状態パラメータが進捗検索欄の初期値になっていません")
                elif int(query_result.get("open", 0)) != 1:
                    page_errors.append("状態パラメータで正本一覧1区分だけが開きません")
                elif int(query_result.get("visible", 0)) != 20:
                    page_errors.append("外部レビュー済の正本一覧が20行ではありません")
            if label == "about":
                if metrics.get("aboutSteps", 0) < 6:
                    page_errors.append("GitHub初心者向け手順のol表示が不足しています")
                about_title_phrases = metrics.get("aboutTitlePhrases", [])
                if len(about_title_phrases) != 2 or any(
                    not isinstance(item, dict)
                    or int(item.get("rectCount", 0)) != 1
                    or float(item.get("width", viewport["width"] + 1)) > viewport["width"]
                    for item in about_title_phrases
                ):
                    page_errors.append("GitHub案内見出しが文節途中で折れています")
                if metrics.get("glossaryTerms") != metrics.get("glossaryDefinitions") or metrics.get("glossaryTerms", 0) < 4:
                    page_errors.append("GitHub用語のdl表示が不足しています")
            if label == "mathml":
                mathml = metrics.get("mathml")
                if not isinstance(mathml, dict) or not mathml.get("present"):
                    page_errors.append("静的MathMLがページ内にありません")
                elif not mathml.get("staticRuntimeFree"):
                    page_errors.append("MathMLページが外部数式ランタイムを参照しています")
            if label == "not-found":
                if metrics.get("navigationResponseStatus") != 404:
                    page_errors.append(
                        f"任意パスが404.htmlのHTTP 404になっていません: {metrics.get('navigationResponseStatus')}"
                    )
                not_found = metrics.get("notFoundTitle")
                if not isinstance(not_found, dict) or not not_found.get("visible"):
                    page_errors.append("404.htmlの案内見出しが表示されていません")

            if label == "top":
                skip_result = evaluate(
                    pipe,
                    session,
                    "new Promise((resolve) => { document.querySelector('.skip-link')?.click(); requestAnimationFrame(() => requestAnimationFrame(() => resolve({activeId: document.activeElement?.id || '', hash: location.hash}))); })",
                )
                if (
                    not isinstance(skip_result, dict)
                    or skip_result.get("activeId") != "main-content"
                    or skip_result.get("hash") != "#main-content"
                ):
                    page_errors.append("スキップリンクが本文へ実フォーカス移動しません")
                set_media(pipe, session, "screen", "dark")
                dark_top = evaluate(pipe, session, APPEARANCE_SCRIPT)
                try:
                    button_contrast = contrast_ratio(
                        dark_top.get("primaryButtonColor") if isinstance(dark_top, dict) else None,
                        dark_top.get("primaryButtonBackground") if isinstance(dark_top, dict) else None,
                    )
                except ValueError as exc:
                    page_errors.append(f"ダークモード主ボタンのコントラストを測定できません: {exc}")
                else:
                    if button_contrast < 4.5:
                        page_errors.append(
                            f"ダークモード主ボタンのコントラスト不足: {button_contrast:.2f}:1"
                        )
                set_media(pipe, session, "screen", color_scheme)

            if label == "lesson-wide-svg":
                set_media(pipe, session, "screen", "dark")
                dark = evaluate(pipe, session, APPEARANCE_SCRIPT)
                if not isinstance(dark, dict) or not dark.get("dark"):
                    page_errors.append("OS追従ダークモードがCDPで有効になりません")
                elif is_white(dark.get("rootBackground")):
                    page_errors.append("ダークモードで閲覧面がライトのままです")
                elif not is_white(dark.get("figureBackground")):
                    page_errors.append("ダークモードで図版の用紙が白く保たれていません")
                set_media(pipe, session, "print")
                printed = evaluate(pipe, session, APPEARANCE_SCRIPT)
                if (
                    not isinstance(printed, dict)
                    or not printed.get("print")
                    or not is_white(printed.get("rootBackground"))
                    or not is_white(printed.get("bodyBackground"))
                ):
                    page_errors.append("print mediaで必ずライト表示になっていません")
                set_media(pipe, session, "screen", color_scheme)

            runtime_errors = event_errors(pipe.events, event_start, session)
            if label == "not-found":
                expected_document_error_seen = False
                retained_errors: list[str] = []
                for error in runtime_errors:
                    if (
                        not expected_document_error_seen
                        and "server responded with a status of 404" in error
                    ):
                        expected_document_error_seen = True
                        continue
                    retained_errors.append(error)
                runtime_errors = retained_errors
            page_errors.extend(runtime_errors)
            pages.append(
                {
                    "label": label,
                    "file": str(path.relative_to(ROOT)),
                    "screenshot": str(screenshot_path.relative_to(ROOT)),
                    "screenshot_origin_y": screenshot_origin_y,
                    "metrics": metrics,
                    "errors": page_errors,
                }
            )
            errors.extend(f"{label}: {error}" for error in page_errors)

        report["pages"] = pages
    except Exception as exc:  # noqa: BLE001 - verifier must report its own failure
        errors.append(str(exc))
    finally:
        if pipe is not None:
            pipe.close()

    report["errors"] = errors
    report["status"] = "ok" if not errors else "failed"
    report_name = (
        f"browser-check-{mode}{'-dark' if args.dark else ''}-report.json"
    )
    report_path = REVIEW_DIR / report_name
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"実描画: {len(report.get('pages', []))}/{len(page_specs) if 'page_specs' in locals() else len(PAGES)}ページ、"
        f"CSS viewport {viewport['width']}×{viewport['height']}、"
        f"エラー{len(errors)}"
    )
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
