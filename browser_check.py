#!/usr/bin/env python3
"""ローカルのChromium/Chromeを使う実描画検査（外部通信・追加依存なし）。"""

from __future__ import annotations

import base64
import argparse
from datetime import datetime, timezone
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
import tempfile
import threading
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
REVIEW_DIR = ROOT / "review" / "browser"
MOBILE_VIEWPORT = {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}
NARROW_MOBILE_VIEWPORT = {"width": 320, "height": 844, "deviceScaleFactor": 1, "mobile": True}
DESKTOP_VIEWPORT = {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False}
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PAGES = (
    ("top", ROOT / "index.html"),
    ("browse", ROOT / "browse/index.html"),
    (
        "diagnostic",
        ROOT
        / "content/materials/jhs-math-3/jhs-math-3-diagnostic/diagnostic.html",
    ),
    (
        "english",
        ROOT
        / "content/materials/jhs-eng-1/jhs-eng-1-introducing-yourself-and-others/lesson_01.html",
    ),
    (
        "lesson-wide-svg",
        ROOT
        / "content/materials/jhs-sci-2/jhs-sci-2-humidity-calculation/lesson_03.html",
    ),
    ("progress", ROOT / "progress/index.html"),
    ("curriculum", ROOT / "curriculum/index.html"),
    ("curriculum-preparing", ROOT / "curriculum/hs-eng/index.html"),
    ("about", ROOT / "about/index.html"),
    ("updates", ROOT / "updates/index.html"),
    (
        "mathml",
        ROOT / "content/materials/jhs-math-3/jhs-math-3-similar-figures/lesson_10.html",
    ),
    (
        "unit-resources",
        ROOT / "units/jhs-math-1-positive-negative-numbers/index.html",
    ),
    (
        "unit-resources-empty",
        ROOT / "units/jhs-math-3-appendix/index.html",
    ),
)
NOT_FOUND_ROUTE = "browser-check-not-found"


def viewport_argument(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9]\d{1,3})x([1-9]\d{1,3})", value)
    if not match:
        raise argparse.ArgumentTypeError("viewportは WIDTHxHEIGHT 形式で指定してください")
    width, height = (int(part) for part in match.groups())
    if not (280 <= width <= 2560 and 320 <= height <= 2560):
        raise argparse.ArgumentTypeError(
            "viewportは幅280〜2560px、高さ320〜2560pxの範囲で指定してください"
        )
    return width, height


def profile_id_argument(value: str) -> str:
    if not PROFILE_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "profile-idは小文字英数字と単一ハイフンだけで指定してください"
        )
    return value


def text_scale_argument(value: str) -> float:
    try:
        scale = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("text-scaleは数値で指定してください") from exc
    if not 1.0 <= scale <= 2.0:
        raise argparse.ArgumentTypeError("text-scaleは1.0〜2.0で指定してください")
    return scale


def expected_update_entries() -> int:
    try:
        report = json.loads((ROOT / "build-report.json").read_text(encoding="utf-8"))
        entries = report.get("update_history", {}).get("entries", [])
        return len(entries) if isinstance(entries, list) else 0
    except (OSError, json.JSONDecodeError, AttributeError):
        return 0


EXPECTED_UPDATE_ENTRIES = expected_update_entries()


def build_report_source_commit() -> str:
    try:
        report = json.loads((ROOT / "build-report.json").read_text(encoding="utf-8"))
        source = report.get("source", {})
        return str(source.get("commit", "")) if isinstance(source, dict) else ""
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def expected_home_grid() -> tuple[int, int, int]:
    try:
        report = json.loads((ROOT / "build-report.json").read_text(encoding="utf-8"))
        home_grid = report.get("curriculum_grid", {})
        families = home_grid.get("families", [])
        packages = home_grid.get("total_packages", 0)
        preparing = home_grid.get("preparing_entries", 0)
        return (
            len(families) if isinstance(families, list) else 0,
            int(packages) if isinstance(packages, int) else 0,
            int(preparing) if isinstance(preparing, int) else 0,
        )
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return (0, 0, 0)


EXPECTED_HOME_FAMILIES, EXPECTED_HOME_PACKAGES, EXPECTED_PREPARING_FAMILIES = (
    expected_home_grid()
)


def find_chrome() -> str:
    playwright_shells = sorted(
        (Path.home() / "Library/Caches/ms-playwright").glob(
            "chromium_headless_shell-*/chrome-mac/headless_shell"
        ),
        reverse=True,
    )
    candidates = tuple(str(path) for path in playwright_shells) + (
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
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
        self.profile = tempfile.TemporaryDirectory(prefix="manabigrid-browser-check-")
        args = [
            chrome,
            "--headless=new",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile.name}",
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
            self.profile.cleanup()
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
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            finally:
                self.profile.cleanup()

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
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      text: (element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120),
      href: element.getAttribute('href') || '',
      target: element.getAttribute('target') || '',
      tabindex: element.getAttribute('tabindex') || '',
      role: element.getAttribute('role') || '',
      ariaLabel: element.getAttribute('aria-label') || '',
      value: 'value' in element ? element.value : '',
    };
  };
  const localScrollers = [...document.querySelectorAll('.figure-scroll, .table-wrap, .math-block, pre')]
    .filter(visible)
    .map((element) => ({
      class: element.className || element.tagName.toLowerCase(),
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      overflowX: getComputedStyle(element).overflowX,
      tabindex: element.getAttribute('tabindex') || '',
      role: element.getAttribute('role') || '',
      label: element.getAttribute('aria-label') || '',
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
    rootFontSize: getComputedStyle(root).fontSize,
    bodyFontSize: body ? getComputedStyle(body).fontSize : '',
    focusableCount: focusable.length,
    firstFocusable: focusable[0] || null,
    header: inspect('.site-header'),
    brand: inspect('.brand-link'),
    navigation: inspect('.site-links'),
    themeControl: inspect('[data-theme-select]'),
    themeMode: document.querySelector('[data-theme-select]')?.value || '',
    pageEndNavigation: inspect('.page-end-nav'),
    pageEndTop: inspect('[data-page-top-link]'),
    pageEndDestination: inspect('.page-end-nav a:not([data-page-top-link])'),
    firstLearningGridItem: inspect('.curriculum-grid .curriculum-grid-item'),
    homeLearningGridTitle: inspect('.learning-grid-section .section-head h2'),
    homeLearningGridCount: document.querySelectorAll('.learning-grid-section .curriculum-grid-item').length,
    homeLearningGridPackageCount: [...document.querySelectorAll('.learning-grid-section .curriculum-grid-item')].reduce((sum, item) => sum + Number(item.dataset.packageCount || 0), 0),
    homePreparingGridCount: document.querySelectorAll('.learning-grid-section .curriculum-grid-item[data-availability="preparing"]').length,
    homeLearningGridPrimary: inspect('.hero-actions .primary-action[href="#learning-grid"]'),
    homeLegacyRouteCount: document.querySelectorAll('.page-home .learning-route').length,
    homeUpdateCount: document.querySelectorAll('.home-updates [data-update-commit]').length,
    homeUpdateListRole: document.querySelector('.home-updates .updates-list')?.getAttribute('role') || '',
    homeUpdatesAllLink: inspect('.home-updates .updates-all-link'),
    updatesTitle: inspect('#updates-title'),
    updateEntryCount: document.querySelectorAll('.page-updates [data-update-commit]').length,
    updateListRole: document.querySelector('.page-updates .updates-list')?.getAttribute('role') || '',
    updateTimeCount: document.querySelectorAll('.page-updates [data-update-commit] time[datetime]').length,
    updateCommitLinkCount: document.querySelectorAll('.page-updates [data-update-commit] a[href*="/commit/"]').length,
    heroPhrases: [...document.querySelectorAll('.hero-phrase')].map((element) => {
      const rect = element.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(element);
      const textRects = [...range.getClientRects()].filter((item) => item.width > 0 && item.height > 0);
      return { text: (element.textContent || '').trim(), width: rect.width, top: rect.top, bottom: rect.bottom, rectCount: textRects.length };
    }),
    readableNow: inspect('.readable-now'),
    readableNowTitle: inspect('#readable-now-title'),
    readableAction: inspect('.readable-actions .button'),
    diagnosticStart: inspect('.diagnostic-start'),
    diagnosticStartAction: inspect('.diagnostic-start .button'),
    figureSource: inspect('.figure-source'),
    figureHint: inspect('[data-scroll-hint]'),
    figureDialog: inspect('[data-figure-dialog]'),
    notFoundTitle: inspect('#not-found-title'),
    notFoundPrimaryAction: inspect('.not-found-actions .button'),
    notFoundSecondaryAction: inspect('.not-found-actions a:not(.button)'),
    lessonSidebar: inspect('.lesson-sidebar'),
    mobileSectionNav: inspect('.mobile-section-nav'),
    englishBlockquote: inspect('blockquote [lang="en"], blockquote[lang="en"]'),
    aboutSteps: document.querySelectorAll('.about-steps li').length,
    aboutStepTexts: [...document.querySelectorAll('.about-step-text')].map((element) => {
      const rect = element.getBoundingClientRect();
      return { width: rect.width, height: rect.height, text: (element.textContent || '').trim() };
    }),
    aboutStepHasAnonymousText: [...document.querySelectorAll('.about-steps li')].some((item) =>
      [...item.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && (node.textContent || '').trim())
    ),
    aboutTitlePhrases: [...document.querySelectorAll('.about-hero .title-phrase')].map((element) => {
      const rect = element.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(element);
      const textRects = [...range.getClientRects()].filter((item) => item.width > 0 && item.height > 0);
      return { text: (element.textContent || '').trim(), width: rect.width, rectCount: textRects.length };
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
    curriculumFamilyCount: document.querySelectorAll('.page-curriculum-index .curriculum-grid-item').length,
    curriculumPreparingCount: document.querySelectorAll('.page-curriculum-index .curriculum-grid-item[data-availability="preparing"]').length,
    curriculumPreparingTitle: inspect('#preparing-note-title'),
    curriculumTrackCount: document.querySelectorAll('.page-curriculum-family .curriculum-track').length,
    curriculumUnitCount: document.querySelectorAll('.page-curriculum-family [data-curriculum-id]').length,
    curriculumFalseAvailableCount: document.querySelectorAll('.page-curriculum-family [data-material="available"]').length,
    tableWraps: [...document.querySelectorAll('.table-wrap')].map((element) => ({
      visible: visible(element),
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      tabindex: element.getAttribute('tabindex') || '',
      role: element.getAttribute('role') || '',
      label: element.getAttribute('aria-label') || '',
    })),
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
  const effectiveBackground = (selector) => {
    let element = document.querySelector(selector);
    while (element) {
      const value = getComputedStyle(element).backgroundColor;
      if (value && value !== 'transparent' && value !== 'rgba(0, 0, 0, 0)') return value;
      element = element.parentElement;
    }
    return color('body', 'backgroundColor');
  };
  return {
    dark: matchMedia('(prefers-color-scheme: dark)').matches,
    print: matchMedia('print').matches,
    rootBackground: color('html', 'backgroundColor'),
    bodyBackground: color('body', 'backgroundColor'),
    figureBackground: color('.svg-figure', 'backgroundColor'),
    figureColor: color('.svg-figure', 'color'),
    figureHintColor: color('.figure-scroll-hint', 'color'),
    figureSourceColor: color('.figure-source', 'color'),
    primaryButtonBackground: color('.button, .primary-action', 'backgroundColor'),
    primaryButtonColor: color('.button, .primary-action', 'color'),
    learningGridHeadingColor: color('.curriculum-grid-item h3 a', 'color'),
    learningGridHeadingBackground: effectiveBackground('.curriculum-grid-item h3 a'),
    learningGridSlotBorder: color('.curriculum-availability', 'borderTopColor'),
    learningGridSlotBackground: effectiveBackground('.curriculum-availability'),
    learningGridItemBreakInside: color('.curriculum-grid-item', 'breakInside'),
  };
})()
"""

THEME_INTERACTION_SCRIPT = r"""
new Promise((resolve) => {
  const select = document.querySelector('[data-theme-select]');
  if (!select) return resolve(null);
  const saved = () => {
    try { return localStorage.getItem('manabigrid-theme'); }
    catch (_error) { return 'storage-unavailable'; }
  };
  const snapshot = () => ({
    value: select.value,
    attribute: document.documentElement.getAttribute('data-theme'),
    saved: saved(),
    background: getComputedStyle(document.body).backgroundColor,
  });
  const choose = (value) => {
    select.value = value;
    select.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const initial = snapshot();
  choose('dark');
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const dark = snapshot();
    choose('light');
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const light = snapshot();
      choose('system');
      requestAnimationFrame(() => requestAnimationFrame(() => {
        resolve({initial, dark, light, system: snapshot()});
      }));
    }));
  }));
})
"""

FIGURE_DIALOG_INTERACTION_SCRIPT = r"""
new Promise((resolve) => {
  const link = document.querySelector('[data-figure-open]');
  const dialog = document.querySelector('[data-figure-dialog]');
  const image = dialog?.querySelector('[data-figure-dialog-image]');
  const close = dialog?.querySelector('[data-figure-close]');
  if (!link || !dialog || !image || !close) return resolve(null);
  const root = document.documentElement;
  const previousBehavior = root.style.scrollBehavior;
  root.style.scrollBehavior = 'auto';
  link.scrollIntoView({block: 'center'});
  root.style.scrollBehavior = previousBehavior;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const beforeY = scrollY;
    const beforeUrl = location.href;
    link.click();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const opened = {
        open: dialog.open,
        urlUnchanged: location.href === beforeUrl,
        imageSource: image.getAttribute('src') || '',
        activeIsClose: document.activeElement === close,
        rootLocked: root.classList.contains('has-modal'),
      };
      close.click();
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const closed = {
          open: dialog.open,
          activeIsLink: document.activeElement === link,
          scrollDelta: Math.abs(scrollY - beforeY),
          rootLocked: root.classList.contains('has-modal'),
        };
        link.click();
        requestAnimationFrame(() => requestAnimationFrame(() => {
          close.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Escape',
            bubbles: true,
          }));
          requestAnimationFrame(() => requestAnimationFrame(() => {
            const escapeClosed = !dialog.open;
            const escapeReturnedFocus = document.activeElement === link;
            resolve({
              opened,
              closed,
              escapeClosed,
              escapeReturnedFocus,
              target: link.getAttribute('target') || '',
            });
          }));
        }));
      }));
    }));
  }));
})
"""

FIGURE_DIALOG_BACKDROP_PREP_SCRIPT = r"""
new Promise((resolve) => {
  const link = document.querySelector('[data-figure-open]');
  const dialog = document.querySelector('[data-figure-dialog]');
  if (!link || !dialog) return resolve(null);
  link.scrollIntoView({block: 'center'});
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const beforeY = scrollY;
    link.click();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const rect = dialog.getBoundingClientRect();
      const x = Math.max(1, Math.floor(rect.left / 2));
      const y = Math.max(1, Math.floor(rect.top / 2));
      resolve({
        open: dialog.open,
        x,
        y,
        targetIsDialog: document.elementFromPoint(x, y) === dialog,
        beforeY,
      });
    }));
  }));
})
"""

FIGURE_DIALOG_BACKDROP_RESULT_SCRIPT = r"""
new Promise((resolve) => {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    resolve({
      open: document.querySelector('[data-figure-dialog]')?.open || false,
      activeIsLink: document.activeElement?.matches?.('[data-figure-open]') || false,
      scrollY,
      rootLocked: document.documentElement.classList.contains('has-modal'),
    });
  }));
})
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


def apply_text_scale(
    pipe: DevToolsSocket,
    session: str,
    scale: float,
) -> dict[str, float]:
    if scale == 1.0:
        return {
            "requestedScale": scale,
            "rootBaselinePx": 0.0,
            "bodyBaselinePx": 0.0,
            "rootAppliedPx": 0.0,
            "bodyAppliedPx": 0.0,
        }
    result = evaluate(
        pipe,
        session,
        (
            "(async () => {"
            "const root = document.documentElement;"
            "const body = document.body;"
            "const rootPx = Number.parseFloat(getComputedStyle(root).fontSize);"
            "const bodyPx = Number.parseFloat(getComputedStyle(body).fontSize);"
            f"root.style.fontSize = `${{rootPx * {scale!r}}}px`;"
            f"body.style.fontSize = `${{bodyPx * {scale!r}}}px`;"
            "window.dispatchEvent(new Event('resize'));"
            "await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));"
            "return {"
            f"requestedScale: {scale!r},"
            "rootBaselinePx: rootPx,"
            "bodyBaselinePx: bodyPx,"
            "rootAppliedPx: Number.parseFloat(getComputedStyle(root).fontSize),"
            "bodyAppliedPx: Number.parseFloat(getComputedStyle(body).fontSize)"
            "};"
            "})()"
        ),
    )
    return validate_text_scale_result(result, scale)


def validate_text_scale_result(
    result: object,
    scale: float,
) -> dict[str, float]:
    """Prove that both root and body actually received the requested scale."""

    keys = (
        "requestedScale",
        "rootBaselinePx",
        "bodyBaselinePx",
        "rootAppliedPx",
        "bodyAppliedPx",
    )
    if not isinstance(result, dict):
        raise RuntimeError("文字拡大のcomputed styleを確認できません")
    try:
        metrics = {key: float(result[key]) for key in keys}
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("文字拡大のcomputed styleを確認できません") from exc
    if abs(metrics["requestedScale"] - scale) > 0.001:
        raise RuntimeError("文字拡大の要求倍率と計測倍率が一致しません")
    for target in ("root", "body"):
        baseline = metrics[f"{target}BaselinePx"]
        applied = metrics[f"{target}AppliedPx"]
        expected = baseline * scale
        tolerance = max(0.05, expected * 0.01)
        if baseline <= 0 or abs(applied - expected) > tolerance:
            raise RuntimeError(
                f"文字拡大が{target}へ要求どおり適用されていません: "
                f"baseline={baseline}px, applied={applied}px, "
                f"expected={expected}px"
            )
    return metrics


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


def set_viewport(
    pipe: DevToolsSocket,
    session: str,
    viewport: dict[str, int | bool],
) -> None:
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
    viewport_group = parser.add_mutually_exclusive_group()
    viewport_group.add_argument("--desktop", action="store_true")
    viewport_group.add_argument(
        "--viewport",
        type=viewport_argument,
        metavar="WIDTHxHEIGHT",
        help="任意のCSS viewportを指定する",
    )
    parser.add_argument(
        "--profile-id",
        type=profile_id_argument,
        help="レポートと画像を識別する安定ID",
    )
    parser.add_argument(
        "--text-scale",
        type=text_scale_argument,
        default=1.0,
        metavar="SCALE",
        help="文字だけを1.0〜2.0倍へ拡大する（既定1.0）",
    )
    parser.add_argument("--dark", action="store_true", help="OSダーク配色をエミュレートして撮影")
    parser.add_argument(
        "--base-url",
        help=(
            "localhostで配信中のサイトroot URL"
            "（例: http://127.0.0.1:8765/manabigrid-site）"
        ),
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
    if args.viewport:
        width, height = args.viewport
        viewport = {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
            "mobile": width <= 900,
        }
        mode = "custom"
    else:
        mode = "desktop" if args.desktop else "mobile"
        viewport = DESKTOP_VIEWPORT if mode == "desktop" else MOBILE_VIEWPORT
    color_scheme = "dark" if args.dark else "light"
    profile_id = args.profile_id
    if profile_id is None and args.viewport:
        profile_id = f"viewport-{viewport['width']}x{viewport['height']}"
        if args.text_scale != 1.0:
            profile_id += f"-text-{round(args.text_scale * 100)}"
    chrome = find_chrome()
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "build_report_source_commit": build_report_source_commit(),
        "status": "ok",
        "chrome": chrome,
        "mode": mode,
        "profile_id": profile_id,
        "text_scale": args.text_scale,
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
            set_viewport(pipe, session, viewport)
            event_start = len(pipe.events)
            navigation = pipe.call("Page.navigate", {"url": page_url}, session)
            if navigation.get("errorText"):
                errors.append(f"{label}: 読み込み失敗 {navigation['errorText']}")
                continue
            wait_until_complete(pipe, session, page_url)
            pipe.call("Page.bringToFront", session_id=session)
            text_scale_metrics = apply_text_scale(
                pipe,
                session,
                args.text_scale,
            )
            settle_first_paint(pipe, session)
            metrics = evaluate(pipe, session, METRICS_SCRIPT)
            if not isinstance(metrics, dict):
                errors.append(f"{label}: 描画指標を取得できません")
                continue
            metrics["textScale"] = text_scale_metrics
            screenshot_origin_y = 0.0
            if label == "lesson-wide-svg":
                figure_scroller = metrics.get("figureScroller")
                if isinstance(figure_scroller, dict) and figure_scroller.get("present"):
                    screenshot_origin_y = max(
                        0.0, float(figure_scroller.get("top", 0.0)) - 120.0
                    )
            elif label in {"unit-resources", "unit-resources-empty"}:
                resource_origin = evaluate(
                    pipe,
                    session,
                    """
new Promise((resolve) => {
  const details = [...document.querySelectorAll('details.resource-list')];
  details.forEach((item) => {
    if (!item.open) item.querySelector('summary')?.click();
  });
  requestAnimationFrame(() => requestAnimationFrame(() => {
    resolve(details[0]?.getBoundingClientRect().top + scrollY || 0);
  }));
})
""",
                )
                screenshot_origin_y = max(0.0, float(resource_origin) - 120.0)
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
            profile_suffix = f"-{profile_id}" if profile_id else ""
            screenshot_path = REVIEW_DIR / (
                f"{label}-{viewport['width']}x{viewport['height']}"
                f"{profile_suffix}{color_suffix}.png"
            )
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
            elif int(brand.get("scrollWidth", 0)) > int(brand.get("clientWidth", 0)) + 1:
                page_errors.append("共通ヘッダーのブランド名が表示領域からはみ出しています")
            if (
                not isinstance(navigation_metrics, dict)
                or not navigation_metrics.get("visible")
                or "教材をさがす" not in str(navigation_metrics.get("text", ""))
                or "進捗一覧" not in str(navigation_metrics.get("text", ""))
            ):
                page_errors.append("共通サイトナビが可視になっていません")
            elif int(navigation_metrics.get("scrollWidth", 0)) > int(
                navigation_metrics.get("clientWidth", 0)
            ) + 1:
                page_errors.append("共通サイトナビが表示領域からはみ出しています")
            theme_control = metrics.get("themeControl")
            if (
                not isinstance(theme_control, dict)
                or not theme_control.get("visible")
                or float(theme_control.get("height", 0)) < 44
                or metrics.get("themeMode") not in {"system", "light", "dark"}
            ):
                page_errors.append("表示テーマの選択肢が共通ヘッダーで操作可能ではありません")
            page_end_navigation = metrics.get("pageEndNavigation")
            page_end_top = metrics.get("pageEndTop")
            page_end_destination = metrics.get("pageEndDestination")
            if (
                not isinstance(page_end_navigation, dict)
                or not page_end_navigation.get("visible")
                or not isinstance(page_end_top, dict)
                or page_end_top.get("href") != "#page-top"
                or float(page_end_top.get("height", 0)) < 44
                or not isinstance(page_end_destination, dict)
                or not page_end_destination.get("href")
                or float(page_end_destination.get("height", 0)) < 44
            ):
                page_errors.append("ページ末の先頭・主要入口への回復導線が不足しています")
            mobile_nav_interaction: dict[str, object] | None = None
            mobile_nav = metrics.get("mobileSectionNav")
            if isinstance(mobile_nav, dict) and mobile_nav.get("visible"):
                interaction = evaluate(
                    pipe,
                    session,
                    """
new Promise((resolve) => {
  const details = document.querySelector('.mobile-section-nav');
  const summary = details?.querySelector('summary');
  if (!details || !summary) return resolve(null);
  const links = [...details.querySelectorAll('a[href*="#"]')];
  const targetExists = (link) => {
    try {
      const hash = new URL(link.href, location.href).hash;
      return Boolean(hash && document.getElementById(decodeURIComponent(hash.slice(1))));
    } catch (_) {
      return false;
    }
  };
  summary.click();
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const openLabel = details.querySelector('.disclosure-label.is-open');
    const last = links.at(-1);
    const lastRect = last?.getBoundingClientRect();
    const result = {
      open: details.open,
      labelVisible: Boolean(
        openLabel
        && getComputedStyle(openLabel).display !== 'none'
        && openLabel.getBoundingClientRect().height > 0
      ),
      linkCount: links.length,
      targetCount: links.filter(targetExists).length,
      lastLinkRendered: Boolean(lastRect && lastRect.height > 0 && lastRect.width > 0),
      lastHref: last?.getAttribute('href') || '',
    };
    summary.click();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      result.closed = !details.open;
      resolve(result);
    }));
  }));
})
""",
                )
                if isinstance(interaction, dict):
                    mobile_nav_interaction = interaction
                    metrics["mobileSectionNavInteraction"] = interaction
                if (
                    not isinstance(interaction, dict)
                    or interaction.get("open") is not True
                    or interaction.get("closed") is not True
                    or interaction.get("labelVisible") is not True
                    or int(interaction.get("linkCount", 0)) == 0
                    or interaction.get("linkCount") != interaction.get("targetCount")
                    or interaction.get("lastLinkRendered") is not True
                ):
                    page_errors.append(
                        "折りたたみ目次の開閉・リンク・最終節を確認できません"
                    )
            first = metrics.get("firstFocusable")
            if not isinstance(first, dict) or "skip-link" not in str(first.get("class", "")).split():
                page_errors.append("最初のフォーカス対象がskip linkではありません")
            table_wraps = metrics.get("tableWraps", [])
            if any(
                isinstance(item, dict)
                and item.get("visible")
                and int(item.get("scrollWidth", 0)) <= int(item.get("clientWidth", 0)) + 1
                and item.get("tabindex") == "0"
                for item in table_wraps
            ):
                page_errors.append("横スクロール不要の表が余分なTab停止になっています")
            if any(
                isinstance(item, dict)
                and item.get("visible")
                and int(item.get("scrollWidth", 0)) > int(item.get("clientWidth", 0)) + 1
                and (item.get("tabindex") != "0" or item.get("role") != "region" or not item.get("label"))
                for item in table_wraps
            ):
                page_errors.append("横スクロール可能な表にフォーカスと名前がありません")
            if label == "lesson-wide-svg" and viewport["mobile"]:
                scrollers = metrics.get("localScrollers", [])
                figure_scrollers = [
                    item
                    for item in scrollers
                    if isinstance(item, dict)
                    and "figure-scroll" in str(item.get("class", "")).split()
                ]
                invalid_wide_scroller = any(
                    isinstance(item, dict)
                    and "figure-scroll" in str(item.get("class", "")).split()
                    and int(item.get("scrollWidth", 0)) > int(item.get("clientWidth", 0))
                    and item.get("overflowX") != "auto"
                    for item in figure_scrollers
                )
                if not figure_scrollers or invalid_wide_scroller:
                    page_errors.append("wide SVGが局所横スクロールになっていません")
                if any(
                    int(item.get("scrollWidth", 0)) > int(item.get("clientWidth", 0)) + 1
                    and (
                        item.get("tabindex") != "0"
                        or item.get("role") != "region"
                        or not item.get("label")
                    )
                    for item in figure_scrollers
                ):
                    page_errors.append("横スクロール可能な図にフォーカスと読み上げ名がありません")
                figure_source = metrics.get("figureSource")
                if (
                    not isinstance(figure_source, dict)
                    or float(figure_source.get("height", 0)) < 44
                    or figure_source.get("target") != "_blank"
                ):
                    page_errors.append("図を大きく見る導線または新規タブfallbackが不足しています")
                figure_hint = metrics.get("figureHint")
                if (
                    any(
                        int(item.get("scrollWidth", 0)) > int(item.get("clientWidth", 0)) + 1
                        for item in figure_scrollers
                    )
                    and (
                        not isinstance(figure_hint, dict)
                        or not figure_hint.get("visible")
                    )
                ):
                    page_errors.append("横スクロール可能な図の操作ヒントが表示されません")
            if label == "lesson-wide-svg":
                dialog_interaction = evaluate(
                    pipe,
                    session,
                    FIGURE_DIALOG_INTERACTION_SCRIPT,
                )
                metrics["figureDialogInteraction"] = dialog_interaction
                if (
                    not isinstance(dialog_interaction, dict)
                    or dialog_interaction.get("target") != "_blank"
                    or dialog_interaction.get("escapeClosed") is not True
                    or dialog_interaction.get("escapeReturnedFocus") is not True
                    or not isinstance(dialog_interaction.get("opened"), dict)
                    or dialog_interaction["opened"].get("open") is not True
                    or dialog_interaction["opened"].get("urlUnchanged") is not True
                    or not dialog_interaction["opened"].get("imageSource")
                    or dialog_interaction["opened"].get("activeIsClose") is not True
                    or dialog_interaction["opened"].get("rootLocked") is not True
                    or not isinstance(dialog_interaction.get("closed"), dict)
                    or dialog_interaction["closed"].get("open") is not False
                    or dialog_interaction["closed"].get("activeIsLink") is not True
                    or float(dialog_interaction["closed"].get("scrollDelta", 99)) > 2
                    or dialog_interaction["closed"].get("rootLocked") is not False
                ):
                    page_errors.append(
                        "図モーダルの表示・閉じる・背景クリック・位置／フォーカス復帰を確認できません"
                    )
                if not viewport["mobile"]:
                    backdrop_prep = evaluate(
                        pipe,
                        session,
                        FIGURE_DIALOG_BACKDROP_PREP_SCRIPT,
                    )
                    metrics["figureBackdropPrep"] = backdrop_prep
                    if (
                        not isinstance(backdrop_prep, dict)
                        or backdrop_prep.get("open") is not True
                        or backdrop_prep.get("targetIsDialog") is not True
                    ):
                        page_errors.append(
                            "図モーダル外側の黒い背景を実座標で特定できません"
                        )
                    else:
                        x = float(backdrop_prep["x"])
                        y = float(backdrop_prep["y"])
                        for event_type in ("mousePressed", "mouseReleased"):
                            pipe.call(
                                "Input.dispatchMouseEvent",
                                {
                                    "type": event_type,
                                    "x": x,
                                    "y": y,
                                    "button": "left",
                                    "clickCount": 1,
                                },
                                session,
                            )
                        backdrop_result = evaluate(
                            pipe,
                            session,
                            FIGURE_DIALOG_BACKDROP_RESULT_SCRIPT,
                        )
                        metrics["figureBackdropResult"] = backdrop_result
                        if (
                            not isinstance(backdrop_result, dict)
                            or backdrop_result.get("open") is not False
                            or backdrop_result.get("activeIsLink") is not True
                            or backdrop_result.get("rootLocked") is not False
                            or abs(
                                float(backdrop_result.get("scrollY", -99))
                                - float(backdrop_prep.get("beforeY", 99))
                            )
                            > 2
                        ):
                            page_errors.append(
                                "図モーダルの黒い背景を実座標で押して元位置へ戻れません"
                            )
            if label == "top":
                first_grid_item = metrics.get("firstLearningGridItem")
                first_grid_visible_height = (
                    max(
                        0.0,
                        min(
                            float(first_grid_item.get("bottom", 0)),
                            float(viewport["height"]),
                        )
                        - max(float(first_grid_item.get("top", 0)), 0.0),
                    )
                    if isinstance(first_grid_item, dict) and first_grid_item.get("visible")
                    else 0.0
                )
                canonical_mobile_view = (
                    viewport["width"] == MOBILE_VIEWPORT["width"]
                    and viewport["height"] == MOBILE_VIEWPORT["height"]
                    and args.text_scale == 1.0
                )
                if canonical_mobile_view and first_grid_visible_height < 44:
                    page_errors.append(
                        "第一画面に学習グリッドの先頭入口が44px以上見えていません"
                    )
                if metrics.get("homeLearningGridCount") != EXPECTED_HOME_FAMILIES:
                    page_errors.append("トップの学習グリッドが中学・高校5教科の10入口を表示していません")
                if metrics.get("homeLearningGridPackageCount") != EXPECTED_HOME_PACKAGES:
                    page_errors.append("トップの学習グリッドの教材件数がパッケージ数と一致しません")
                if metrics.get("homePreparingGridCount") != EXPECTED_PREPARING_FAMILIES:
                    page_errors.append("トップの準備中入口数が正本由来の件数と一致しません")
                home_grid_primary = metrics.get("homeLearningGridPrimary")
                if (
                    not isinstance(home_grid_primary, dict)
                    or not home_grid_primary.get("visible")
                    or float(home_grid_primary.get("height", 0)) < 44
                ):
                    page_errors.append("トップの学習グリッド主要導線が可視または44px以上になっていません")
                if metrics.get("homeLegacyRouteCount") != 0:
                    page_errors.append("トップに旧中3数学専用ルートが残っています")
                hero_phrases = metrics.get("heroPhrases", [])
                if not hero_phrases or any(
                    not isinstance(item, dict)
                    or (
                        args.text_scale == 1.0
                        and int(item.get("rectCount", 0)) != 1
                    )
                    or float(item.get("width", viewport["width"] + 1)) > viewport["width"]
                    for item in hero_phrases
                ):
                    page_errors.append("ヒーローの句が語中改行なしで収まっていません")
                elif (
                    args.text_scale == 1.0
                    and (viewport["width"], viewport["height"])
                    in {
                        (MOBILE_VIEWPORT["width"], MOBILE_VIEWPORT["height"]),
                        (DESKTOP_VIEWPORT["width"], DESKTOP_VIEWPORT["height"]),
                    }
                ) and (
                    len(hero_phrases) != 3
                    or abs(float(hero_phrases[0].get("top", 0)) - float(hero_phrases[1].get("top", 0))) < 1
                    or abs(float(hero_phrases[1].get("top", 0)) - float(hero_phrases[2].get("top", 0))) > 1
                ):
                    page_errors.append("ヒーロー見出しが意図した文節2行になっていません")
                home_updates_link = metrics.get("homeUpdatesAllLink")
                if metrics.get("homeUpdateCount") != min(3, EXPECTED_UPDATE_ENTRIES):
                    page_errors.append("トップの最近の更新が最新3件になっていません")
                if metrics.get("homeUpdateListRole") != "list":
                    page_errors.append("トップの更新時系列がリストとして公開されていません")
                if (
                    not isinstance(home_updates_link, dict)
                    or not home_updates_link.get("visible")
                    or "更新履歴をすべて見る" not in str(home_updates_link.get("text", ""))
                ):
                    page_errors.append("トップから更新履歴ページへの導線が見つかりません")
                if canonical_mobile_view and args.viewport is None:
                    set_viewport(pipe, session, NARROW_MOBILE_VIEWPORT)
                    settle_first_paint(pipe, session)
                    narrow_metrics = evaluate(pipe, session, METRICS_SCRIPT)
                    metrics["narrow320"] = narrow_metrics
                    if not isinstance(narrow_metrics, dict):
                        page_errors.append("320pxトップの描画指標を取得できません")
                    else:
                        if narrow_metrics.get("innerWidth") != NARROW_MOBILE_VIEWPORT["width"]:
                            page_errors.append(
                                "320pxトップのCSS viewportを再現できません: "
                                f"{narrow_metrics.get('innerWidth')}px"
                            )
                        if narrow_metrics.get("pageOverflow"):
                            page_errors.append(
                                "320pxトップが横にはみ出しています: "
                                f"{narrow_metrics.get('pageWidth')}px"
                            )
                        narrow_heading = narrow_metrics.get("homeLearningGridTitle")
                        narrow_entry = narrow_metrics.get("firstLearningGridItem")
                        heading_visible_height = (
                            max(
                                0.0,
                                min(
                                    float(narrow_heading.get("bottom", 0)),
                                    float(NARROW_MOBILE_VIEWPORT["height"]),
                                )
                                - max(float(narrow_heading.get("top", 0)), 0.0),
                            )
                            if isinstance(narrow_heading, dict) and narrow_heading.get("visible")
                            else 0.0
                        )
                        entry_visible_height = (
                            max(
                                0.0,
                                min(
                                    float(narrow_entry.get("bottom", 0)),
                                    float(NARROW_MOBILE_VIEWPORT["height"]),
                                )
                                - max(float(narrow_entry.get("top", 0)), 0.0),
                            )
                            if isinstance(narrow_entry, dict) and narrow_entry.get("visible")
                            else 0.0
                        )
                        if heading_visible_height < 24 and entry_visible_height < 44:
                            page_errors.append(
                                "320px第一画面に学習グリッド見出しまたは意味ある入口が見えていません"
                            )
                    set_viewport(pipe, session, viewport)
                    settle_first_paint(pipe, session)
            if label == "browse":
                browse_result = evaluate(
                    pipe,
                    session,
                    "new Promise((resolve) => { const input = document.querySelector('[data-content-search-input]'); if (!input) return resolve(null); input.value = '平方根'; input.dispatchEvent(new Event('input', {bubbles: true})); requestAnimationFrame(() => requestAnimationFrame(() => { const items = [...document.querySelectorAll('[data-content-search-list] li')]; const kinds = items.map((item) => (item.querySelector('span')?.textContent || '').split(' ／ ')[0]); resolve({count: items.length, firstKind: kinds[0] || '', firstHref: items[0]?.querySelector('a')?.getAttribute('href') || '', kinds, metas: items.map((item) => item.querySelector('span')?.textContent || '')}); })); })",
                )
                if not isinstance(browse_result, dict) or int(browse_result.get("count", 0)) == 0:
                    page_errors.append("教材横断検索が平方根の候補を表示しません")
                else:
                    kinds = browse_result.get("kinds", [])
                    first_answer = kinds.index("解答") if "解答" in kinds else len(kinds)
                    last_lesson = max((index for index, kind in enumerate(kinds) if kind == "レッスン"), default=-1)
                    if browse_result.get("firstKind") != "レッスン" or "answer" in str(browse_result.get("firstHref", "")).lower():
                        page_errors.append("教材横断検索がレッスンより先に解答を表示しています")
                    if first_answer < last_lesson:
                        page_errors.append("教材横断検索の解答がレッスン群より前に混在しています")
                    if any(" ／ " not in str(meta) for meta in browse_result.get("metas", [])):
                        page_errors.append("教材横断検索の結果種別が表示されていません")
                input_colors = evaluate(
                    pipe,
                    session,
                    "(() => { const input = document.querySelector('.search-input'); if (!input) return null; const style = getComputedStyle(input); return {border: style.borderLeftColor, background: style.backgroundColor}; })()",
                )
                if isinstance(input_colors, dict):
                    try:
                        input_border_contrast = contrast_ratio(input_colors.get("border"), input_colors.get("background"))
                    except ValueError as exc:
                        page_errors.append(f"検索入力欄の境界コントラストを測定できません: {exc}")
                    else:
                        if input_border_contrast < 3:
                            page_errors.append(f"検索入力欄の境界コントラスト不足: {input_border_contrast:.2f}:1")
                capped_status = evaluate(
                    pipe,
                    session,
                    "new Promise((resolve) => { const input = document.querySelector('[data-content-search-input]'); input.value = '数学'; input.dispatchEvent(new Event('input', {bubbles: true})); requestAnimationFrame(() => requestAnimationFrame(() => resolve({shown: document.querySelectorAll('[data-content-search-list] li').length, status: document.querySelector('[data-filter-status]')?.textContent || ''}))); })",
                )
                if (
                    not isinstance(capped_status, dict)
                    or int(capped_status.get("shown", 0)) != 24
                    or "件中24件を表示" not in str(capped_status.get("status", ""))
                ):
                    page_errors.append("検索候補の総件数と24件表示上限が読み上げ文で区別されません")
                heading_labels = evaluate(
                    pipe,
                    session,
                    "new Promise((resolve) => { const input = document.querySelector('[data-content-search-input]'); input.value = 'ねらい'; input.dispatchEvent(new Event('input', {bubbles: true})); requestAnimationFrame(() => requestAnimationFrame(() => resolve([...document.querySelectorAll('[data-content-search-list] a')].map((link) => link.textContent || '')))); })",
                )
                if (
                    not isinstance(heading_labels, list)
                    or len(heading_labels) != 24
                    or len(set(heading_labels)) != len(heading_labels)
                    or any(" — " not in label for label in heading_labels)
                ):
                    page_errors.append("見出し検索のリンク名がレッスン名を含まず一意になっていません")
            if label == "diagnostic":
                diagnostic_start = metrics.get("diagnosticStart")
                diagnostic_action = metrics.get("diagnosticStartAction")
                if (
                    not isinstance(diagnostic_start, dict)
                    or not diagnostic_start.get("visible")
                    or not isinstance(diagnostic_action, dict)
                    or not diagnostic_action.get("visible")
                    or "Q1" not in str(diagnostic_action.get("text", ""))
                ):
                    page_errors.append("診断ページの開始予告とQ1導線が可視になっていません")
                if (
                    isinstance(mobile_nav, dict)
                    and mobile_nav.get("visible")
                    and (
                        not isinstance(mobile_nav_interaction, dict)
                        or mobile_nav_interaction.get("linkCount") != 12
                    )
                ):
                    page_errors.append("診断の折りたたみ目次が12節を表示していません")
            if label == "english":
                english_blockquote = metrics.get("englishBlockquote")
                lesson_sidebar = metrics.get("lessonSidebar")
                compact_lesson_layout = (
                    viewport["width"] <= 1100 or viewport["height"] <= 800
                )
                if not isinstance(english_blockquote, dict) or not english_blockquote.get("present"):
                    page_errors.append("英語4語以上の例文に部分言語lang=enがありません")
                if not isinstance(mobile_nav, dict) or not mobile_nav.get("present"):
                    page_errors.append("6節のレッスンにモバイル用目次が生成されていません")
                elif compact_lesson_layout and not mobile_nav.get("visible"):
                    page_errors.append("低い画面で折りたたみ目次が表示されていません")
                elif not compact_lesson_layout and (
                    not isinstance(lesson_sidebar, dict)
                    or not lesson_sidebar.get("visible")
                ):
                    page_errors.append("十分に広い画面でレッスンサイド目次が表示されていません")
                if (
                    isinstance(mobile_nav, dict)
                    and mobile_nav.get("visible")
                    and (
                        not isinstance(mobile_nav_interaction, dict)
                        or mobile_nav_interaction.get("linkCount") != 8
                    )
                ):
                    page_errors.append(
                        "長いレッスンの折りたたみ目次が8節を表示していません"
                    )
            if label == "progress":
                if metrics.get("progressDisclosureCount") != 12:
                    page_errors.append("進捗の大きな表が12区分の折りたたみになっていません")
                if metrics.get("progressOpenDisclosureCount") != 0:
                    page_errors.append("進捗の表が初期状態で閉じていません")
                readable_now = metrics.get("readableNow")
                readable_now_title = metrics.get("readableNowTitle")
                readable_action = metrics.get("readableAction")
                if (
                    not isinstance(readable_now, dict)
                    or not readable_now.get("visible")
                    or not isinstance(readable_action, dict)
                    or not readable_action.get("visible")
                ):
                    page_errors.append("進捗ページのCTAが可視になっていません")
                if viewport["mobile"] and isinstance(readable_now_title, dict) and (
                    float(readable_now_title.get("left", 0)) < 15
                    or float(readable_now_title.get("right", viewport["width"])) > viewport["width"] - 15
                ):
                    page_errors.append("進捗ページのREAD NOW領域からモバイル左右余白が消えています")
                zero_colors = evaluate(
                    pipe,
                    session,
                    "(() => { const text = document.querySelector('.stat-card.is-zero span'); const card = document.querySelector('.stat-card.is-zero'); if (!text || !card) return null; return {color: getComputedStyle(text).color, background: getComputedStyle(card).backgroundColor}; })()",
                )
                if isinstance(zero_colors, dict):
                    try:
                        zero_contrast = contrast_ratio(zero_colors.get("color"), zero_colors.get("background"))
                    except ValueError as exc:
                        page_errors.append(f"0件カードのコントラストを測定できません: {exc}")
                    else:
                        if zero_contrast < 4.5:
                            page_errors.append(f"0件カードの文字コントラスト不足: {zero_contrast:.2f}:1")
                query_url = f"{page_url}?status=%E5%A4%96%E9%83%A8%E3%83%AC%E3%83%93%E3%83%A5%E3%83%BC%E6%B8%88"
                pipe.call("Page.navigate", {"url": query_url}, session)
                wait_until_complete(pipe, session, query_url)
                settle_first_paint(pipe, session)
                query_result = evaluate(
                    pipe,
                    session,
                    "({value: document.querySelector('[data-filter-input]')?.value || '', open: document.querySelectorAll('[data-progress-disclosure][open]').length, visible: document.querySelectorAll('[data-search-item]:not([hidden])').length, expected: Number([...document.querySelectorAll('.stat-card')].find((card) => card.querySelector('span')?.textContent === '外部レビュー済')?.querySelector('strong')?.textContent || 0), tables: [...document.querySelectorAll('[data-progress-disclosure][open] .table-wrap')].map((element) => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth, tabindex: element.getAttribute('tabindex') || '', role: element.getAttribute('role') || '', label: element.getAttribute('aria-label') || ''}))})",
                )
                if not isinstance(query_result, dict) or query_result.get("value") != "外部レビュー済":
                    page_errors.append("状態パラメータが進捗検索欄の初期値になっていません")
                elif int(query_result.get("open", 0)) != 1:
                    page_errors.append("状態パラメータで正本一覧1区分だけが開きません")
                elif int(query_result.get("expected", 0)) <= 0 or int(query_result.get("visible", 0)) != int(query_result.get("expected", 0)):
                    page_errors.append("外部レビュー済の集計値と正本一覧の表示行数が一致しません")
                else:
                    query_tables = query_result.get("tables", [])
                    if not query_tables:
                        page_errors.append("状態パラメータで開いた進捗表を測定できません")
                    elif any(
                        isinstance(item, dict)
                        and int(item.get("scrollWidth", 0)) > int(item.get("clientWidth", 0)) + 1
                        and (item.get("tabindex") != "0" or item.get("role") != "region" or not item.get("label"))
                        for item in query_tables
                    ):
                        page_errors.append("検索で開いた横スクロール進捗表にフォーカスと名前がありません")
                    elif any(
                        isinstance(item, dict)
                        and int(item.get("scrollWidth", 0)) <= int(item.get("clientWidth", 0)) + 1
                        and item.get("tabindex") == "0"
                        for item in query_tables
                    ):
                        page_errors.append("検索で開いた進捗表に余分なTab停止があります")
            if label == "curriculum":
                if metrics.get("curriculumFamilyCount") != EXPECTED_HOME_FAMILIES:
                    page_errors.append("学習グリッド全体ページが10入口を表示していません")
                if metrics.get("curriculumPreparingCount") != EXPECTED_PREPARING_FAMILIES:
                    page_errors.append("学習グリッド全体ページの準備中入口数が一致しません")
            if label == "curriculum-preparing":
                preparing_title = metrics.get("curriculumPreparingTitle")
                if (
                    not isinstance(preparing_title, dict)
                    or not preparing_title.get("visible")
                    or "準備中" not in str(preparing_title.get("text", ""))
                ):
                    page_errors.append("準備中教科ページの説明が可視になっていません")
                if int(metrics.get("curriculumTrackCount", 0)) <= 0:
                    page_errors.append("準備中教科ページに学年・科目別の骨格がありません")
                if int(metrics.get("curriculumUnitCount", 0)) <= 0:
                    page_errors.append("準備中教科ページに正本単元がありません")
                if int(metrics.get("curriculumFalseAvailableCount", 0)) != 0:
                    page_errors.append("準備中教科ページに偽の教材あり導線があります")
            if label == "about":
                if metrics.get("aboutSteps", 0) < 6:
                    page_errors.append("GitHub初心者向け手順のol表示が不足しています")
                about_title_phrases = metrics.get("aboutTitlePhrases", [])
                if len(about_title_phrases) != 2 or any(
                    not isinstance(item, dict)
                    or (
                        args.text_scale == 1.0
                        and int(item.get("rectCount", 0)) != 1
                    )
                    or float(item.get("width", viewport["width"] + 1)) > viewport["width"]
                    for item in about_title_phrases
                ):
                    page_errors.append("GitHub案内見出しが文節途中で折れています")
                if metrics.get("glossaryTerms") != metrics.get("glossaryDefinitions") or metrics.get("glossaryTerms", 0) < 4:
                    page_errors.append("GitHub用語のdl表示が不足しています")
                step_texts = metrics.get("aboutStepTexts", [])
                if (
                    len(step_texts) != metrics.get("aboutSteps")
                    or any(float(item.get("width", 0)) < (220 if viewport["mobile"] else 560) for item in step_texts if isinstance(item, dict))
                    or metrics.get("aboutStepHasAnonymousText")
                ):
                    page_errors.append("aboutの手順本文が番号列へ分断されています")
                pipe.call(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": 320, "height": 844, "deviceScaleFactor": 1, "mobile": True, "screenWidth": 320, "screenHeight": 844, "positionX": 0, "positionY": 0},
                    session,
                )
                settle_first_paint(pipe, session)
                about_reflow = evaluate(
                    pipe,
                    session,
                    "({innerWidth, scrollWidth: document.documentElement.scrollWidth, phraseRight: Math.max(...[...document.querySelectorAll('.about-hero .title-phrase')].map((item) => item.getBoundingClientRect().right))})",
                )
                if (
                    not isinstance(about_reflow, dict)
                    or float(about_reflow.get("scrollWidth", 321)) > 320
                    or float(about_reflow.get("phraseRight", 321)) > 304
                ):
                    page_errors.append("aboutが320px幅で横にはみ出しています")
                pipe.call(
                    "Emulation.setDeviceMetricsOverride",
                    {**viewport, "screenWidth": viewport["width"], "screenHeight": viewport["height"], "positionX": 0, "positionY": 0},
                    session,
                )
            if label == "updates":
                updates_title = metrics.get("updatesTitle")
                update_count = int(metrics.get("updateEntryCount", 0))
                if (
                    not isinstance(updates_title, dict)
                    or not updates_title.get("visible")
                    or "更新履歴" not in str(updates_title.get("text", ""))
                ):
                    page_errors.append("更新履歴ページの主見出しが表示されていません")
                if update_count != EXPECTED_UPDATE_ENTRIES or update_count < 1:
                    page_errors.append("更新履歴ページに更新がありません")
                elif metrics.get("updateListRole") != "list":
                    page_errors.append("更新履歴の時系列がリストとして公開されていません")
                elif (
                    int(metrics.get("updateTimeCount", 0)) != update_count
                    or int(metrics.get("updateCommitLinkCount", 0)) != update_count
                ):
                    page_errors.append("更新履歴の日付または固定コミットリンクが不足しています")
            if label == "mathml":
                mathml = metrics.get("mathml")
                if not isinstance(mathml, dict) or not mathml.get("present"):
                    page_errors.append("静的MathMLがページ内にありません")
                elif not mathml.get("staticRuntimeFree"):
                    page_errors.append("MathMLページが外部数式ランタイムを参照しています")
            if label in {"unit-resources", "unit-resources-empty"}:
                unit_resources = evaluate(
                    pipe,
                    session,
                    """
new Promise((resolve) => {
  const details = [...document.querySelectorAll('details.resource-list')];
  details.forEach((item) => {
    if (!item.open) item.querySelector('summary')?.click();
  });
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const rows = [...document.querySelectorAll(
      'details.resource-list li.resource-item-plain'
    )].map((row) => {
      const content = row.querySelector(':scope > div');
      const rowRect = row.getBoundingClientRect();
      const contentRect = content?.getBoundingClientRect();
      return {
        rowWidth: rowRect.width,
        contentWidth: contentRect?.width || 0,
        height: rowRect.height,
        scrollWidth: row.scrollWidth,
        clientWidth: row.clientWidth,
      };
    });
    const allRows = [...document.querySelectorAll('.resource-list li')];
    const classifiedRows = allRows.filter((row) =>
      row.classList.contains('resource-item-numbered')
      || row.classList.contains('resource-item-plain')
    );
    const firstLesson = document.querySelector(
      '.primary-resources li.resource-item-numbered'
    );
    const primaryPlain = document.querySelector(
      '.primary-resources li.resource-item-plain'
    );
    resolve({
      disclosureCount: details.length,
      allOpen: details.every((item) => item.open),
      rows,
      allRowCount: allRows.length,
      classifiedRowCount: classifiedRows.length,
      numberedLessonPresent: Boolean(
        firstLesson && firstLesson.querySelector('.resource-number')
      ),
      primaryPlainPresent: Boolean(primaryPlain),
      numberedLessonColumns: firstLesson
        ? getComputedStyle(firstLesson).gridTemplateColumns
        : '',
    });
  }));
})
""",
                )
                metrics["unitResources"] = unit_resources
                rows = (
                    unit_resources.get("rows", [])
                    if isinstance(unit_resources, dict)
                    else []
                )
                max_row_height = 220 * args.text_scale
                if (
                    not isinstance(unit_resources, dict)
                    or unit_resources.get("disclosureCount") != 2
                    or unit_resources.get("allOpen") is not True
                    or unit_resources.get("allRowCount")
                    != unit_resources.get("classifiedRowCount")
                    or not rows
                    or any(
                        not isinstance(row, dict)
                        or float(row.get("contentWidth", 0))
                        < float(row.get("rowWidth", 1)) * 0.95
                        or int(row.get("scrollWidth", 0))
                        > int(row.get("clientWidth", 0)) + 8
                        or float(row.get("height", max_row_height + 1))
                        > max_row_height
                        for row in rows
                    )
                ):
                    page_errors.append(
                        "単元ページの解答・制作資料が本文幅1列で短く表示されていません"
                    )
                if label == "unit-resources":
                    lesson_columns = str(
                        unit_resources.get("numberedLessonColumns", "")
                        if isinstance(unit_resources, dict)
                        else ""
                    ).split()
                    if (
                        not isinstance(unit_resources, dict)
                        or unit_resources.get("numberedLessonPresent") is not True
                        or len(lesson_columns) < 2
                    ):
                        page_errors.append(
                            "単元ページの番号付きレッスン行が2列ではありません"
                        )
                elif (
                    not isinstance(unit_resources, dict)
                    or unit_resources.get("primaryPlainPresent") is not True
                ):
                    page_errors.append(
                        "教材0件の単元ページで空状態が本文幅1列になっていません"
                    )
            if label == "not-found":
                if metrics.get("navigationResponseStatus") != 404:
                    page_errors.append(
                        f"任意パスが404.htmlのHTTP 404になっていません: {metrics.get('navigationResponseStatus')}"
                    )
                not_found = metrics.get("notFoundTitle")
                if not isinstance(not_found, dict) or not not_found.get("visible"):
                    page_errors.append("404.htmlの案内見出しが表示されていません")
                primary_action = metrics.get("notFoundPrimaryAction")
                secondary_action = metrics.get("notFoundSecondaryAction")
                if not isinstance(primary_action, dict) or not isinstance(secondary_action, dict):
                    page_errors.append("404.htmlの回復導線を測定できません")
                else:
                    vertical_overlap = min(float(primary_action.get("bottom", 0)), float(secondary_action.get("bottom", 0))) - max(float(primary_action.get("top", 0)), float(secondary_action.get("top", 0)))
                    action_gap = (
                        max(0.0, float(secondary_action.get("left", 0)) - float(primary_action.get("right", 0)))
                        if vertical_overlap > 0
                        else max(0.0, float(secondary_action.get("top", 0)) - float(primary_action.get("bottom", 0)))
                    )
                    if action_gap < 12:
                        page_errors.append(f"404.htmlの回復導線間隔が不足しています: {action_gap:.1f}px")
                    if float(secondary_action.get("height", 0)) < 44:
                        page_errors.append("404.htmlの副導線のタップ高さが44px未満です")

            if label == "top":
                theme_interaction = evaluate(
                    pipe,
                    session,
                    THEME_INTERACTION_SCRIPT,
                )
                metrics["themeInteraction"] = theme_interaction
                if (
                    not isinstance(theme_interaction, dict)
                    or not isinstance(theme_interaction.get("dark"), dict)
                    or theme_interaction["dark"].get("attribute") != "dark"
                    or theme_interaction["dark"].get("saved")
                    not in {"dark", "storage-unavailable"}
                    or not isinstance(theme_interaction.get("light"), dict)
                    or theme_interaction["light"].get("attribute") != "light"
                    or theme_interaction["light"].get("saved")
                    not in {"light", "storage-unavailable"}
                    or theme_interaction["dark"].get("background")
                    == theme_interaction["light"].get("background")
                    or not isinstance(theme_interaction.get("system"), dict)
                    or theme_interaction["system"].get("attribute") is not None
                    or theme_interaction["system"].get("saved")
                    not in {None, "storage-unavailable"}
                ):
                    page_errors.append(
                        "表示テーマの自動・明るい・暗い切替と保存を確認できません"
                    )
                if not viewport["mobile"] and viewport["height"] > 800:
                    sticky_header = evaluate(
                        pipe,
                        session,
                        """
new Promise((resolve) => {
  const header = document.querySelector('.site-header');
  if (!header) return resolve(null);
  const maxScroll = Math.max(0, document.documentElement.scrollHeight - innerHeight);
  scrollTo(0, Math.min(320, maxScroll));
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const rect = header.getBoundingClientRect();
    const result = {
      position: getComputedStyle(header).position,
      top: rect.top,
      scrollY,
    };
    scrollTo(0, 0);
    requestAnimationFrame(() => requestAnimationFrame(() => resolve(result)));
  }));
})
""",
                    )
                    metrics["stickyHeader"] = sticky_header
                    if (
                        not isinstance(sticky_header, dict)
                        or sticky_header.get("position") != "sticky"
                        or float(sticky_header.get("scrollY", 0)) < 1
                        or abs(float(sticky_header.get("top", 99))) > 1
                    ):
                        page_errors.append(
                            "デスクトップの共通ヘッダーがスクロール時に上端へ固定されません"
                        )
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
                evaluate(
                    pipe,
                    session,
                    "(() => { const select = document.querySelector('[data-theme-select]'); select.value = 'dark'; select.dispatchEvent(new Event('change', {bubbles: true})); return document.documentElement.getAttribute('data-theme'); })()",
                )
                set_media(pipe, session, "print")
                explicit_dark_print = evaluate(pipe, session, APPEARANCE_SCRIPT)
                metrics["explicitDarkPrintAppearance"] = explicit_dark_print
                if (
                    not isinstance(explicit_dark_print, dict)
                    or not explicit_dark_print.get("print")
                    or not is_white(explicit_dark_print.get("rootBackground"))
                    or not is_white(explicit_dark_print.get("bodyBackground"))
                ):
                    page_errors.append("明示ダーク選択から印刷時の白地へ戻りません")
                set_media(pipe, session, "screen", color_scheme)
                evaluate(
                    pipe,
                    session,
                    "(() => { const select = document.querySelector('[data-theme-select]'); select.value = 'system'; select.dispatchEvent(new Event('change', {bubbles: true})); return document.documentElement.getAttribute('data-theme'); })()",
                )
                set_media(pipe, session, "screen", "dark")
                dark_top = evaluate(pipe, session, APPEARANCE_SCRIPT)
                metrics["darkTopAppearance"] = dark_top
                dark_top_contrasts: dict[str, float] = {}
                if not isinstance(dark_top, dict) or not dark_top.get("dark"):
                    page_errors.append("トップのOS追従ダークモードがCDPで有効になりません")
                try:
                    button_contrast = contrast_ratio(
                        dark_top.get("primaryButtonColor") if isinstance(dark_top, dict) else None,
                        dark_top.get("primaryButtonBackground") if isinstance(dark_top, dict) else None,
                    )
                except ValueError as exc:
                    page_errors.append(f"ダークモード主ボタンのコントラストを測定できません: {exc}")
                else:
                    dark_top_contrasts["primaryButton"] = button_contrast
                    if button_contrast < 4.5:
                        page_errors.append(
                            f"ダークモード主ボタンのコントラスト不足: {button_contrast:.2f}:1"
                        )
                for label_text, foreground_key, background_key, minimum in (
                    (
                        "学習グリッド見出し",
                        "learningGridHeadingColor",
                        "learningGridHeadingBackground",
                        4.5,
                    ),
                    (
                        "学習グリッドの升目境界",
                        "learningGridSlotBorder",
                        "learningGridSlotBackground",
                        3.0,
                    ),
                ):
                    try:
                        grid_contrast = contrast_ratio(
                            dark_top.get(foreground_key) if isinstance(dark_top, dict) else None,
                            dark_top.get(background_key) if isinstance(dark_top, dict) else None,
                        )
                    except ValueError as exc:
                        page_errors.append(
                            f"ダークモードの{label_text}コントラストを測定できません: {exc}"
                        )
                    else:
                        dark_top_contrasts[foreground_key] = grid_contrast
                        if grid_contrast < minimum:
                            page_errors.append(
                                f"ダークモードの{label_text}コントラスト不足: "
                                f"{grid_contrast:.2f}:1（必要 {minimum:.1f}:1）"
                            )
                set_media(pipe, session, "print")
                printed_top = evaluate(pipe, session, APPEARANCE_SCRIPT)
                metrics["darkTopContrasts"] = dark_top_contrasts
                metrics["printTopAppearance"] = printed_top
                if (
                    not isinstance(printed_top, dict)
                    or not printed_top.get("print")
                    or not is_white(printed_top.get("rootBackground"))
                    or not is_white(printed_top.get("bodyBackground"))
                ):
                    page_errors.append("トップがprint mediaで白地へ戻っていません")
                if (
                    not isinstance(printed_top, dict)
                    or printed_top.get("learningGridItemBreakInside") != "avoid"
                ):
                    page_errors.append("印刷時の学習グリッド入口が途中改ページを避けていません")
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
                else:
                    for label_text, color_key in (("横スクロールヒント", "figureHintColor"), ("図拡大リンク", "figureSourceColor")):
                        try:
                            figure_contrast = contrast_ratio(dark.get(color_key), dark.get("figureBackground"))
                        except ValueError as exc:
                            page_errors.append(f"ダークモードの{label_text}コントラストを測定できません: {exc}")
                        else:
                            if figure_contrast < 4.5:
                                page_errors.append(f"ダークモードの{label_text}コントラスト不足: {figure_contrast:.2f}:1")
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

            if label in {"top", "progress", "about"}:
                set_media(pipe, session, "print")
                printed_controls = evaluate(pipe, session, APPEARANCE_SCRIPT)
                metrics["printControlsAppearance"] = printed_controls
                try:
                    printed_button_contrast = contrast_ratio(
                        (
                            printed_controls.get("primaryButtonColor")
                            if isinstance(printed_controls, dict)
                            else None
                        ),
                        (
                            printed_controls.get("primaryButtonBackground")
                            if isinstance(printed_controls, dict)
                            else None
                        ),
                    )
                except ValueError as exc:
                    page_errors.append(
                        f"印刷時の主ボタンのコントラストを測定できません: {exc}"
                    )
                else:
                    metrics["printButtonContrast"] = printed_button_contrast
                    if (
                        not is_white(printed_controls.get("primaryButtonBackground"))
                        or printed_button_contrast < 4.5
                    ):
                        page_errors.append(
                            "印刷時の主ボタンが白地・読みやすい文字色へ戻っていません"
                        )
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
    report_key = profile_id or mode
    report_name = f"browser-check-{report_key}{'-dark' if args.dark else ''}-report.json"
    report_path = REVIEW_DIR / report_name
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"実描画: {len(report.get('pages', []))}/{len(page_specs) if 'page_specs' in locals() else len(PAGES)}ページ、"
        f"CSS viewport {viewport['width']}×{viewport['height']}、"
        f"文字 {args.text_scale:.2f}倍、"
        f"エラー{len(errors)}"
    )
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
