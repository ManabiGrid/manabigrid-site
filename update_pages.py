#!/usr/bin/env python3
"""Dispatch and verify one exact ManabiGrid Pages update without dependencies."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "site.config.json").read_text(encoding="utf-8"))
WORKFLOW = "pages.yml"
WORKFLOW_NAME = "Build and deploy ManabiGrid Pages"
REPORT_PATH = ROOT / "update-report.json"
SHA_LENGTH = 40


class UpdateError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RunMatch:
    database_id: int
    title: str
    url: str


def command(arguments: Sequence[str], *, timeout: int = 120) -> str:
    try:
        completed = subprocess.run(
            list(arguments),
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise UpdateError("blocked_missing_tool", f"必要なコマンドがありません: {arguments[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("failed_command_timeout", f"コマンドが時間切れです: {' '.join(arguments)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise UpdateError(
            "failed_command",
            f"コマンドに失敗しました: {' '.join(arguments)}\n{detail}",
        ) from exc
    return completed.stdout.strip()


def is_full_sha(value: str) -> bool:
    return len(value) == SHA_LENGTH and all(character in "0123456789abcdef" for character in value)


def remote_sha(remote: str) -> str:
    output = command(("git", "ls-remote", remote, "refs/heads/main"))
    value = output.split()[0] if output else ""
    if not is_full_sha(value):
        raise UpdateError("blocked_remote_state", f"mainのSHAを取得できません: {remote}")
    return value


def published_report() -> dict[str, Any] | None:
    base = CONFIG["base_url"].rstrip("/") + "/"
    url = urllib.parse.urljoin(base, "build-report.json")
    request = Request(
        url + f"?status={time.time_ns()}",
        headers={"User-Agent": "ManabiGrid-Update-Runner/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            value = json.load(response)
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def published_sha() -> str | None:
    report = published_report()
    if not report:
        return None
    source = report.get("source")
    value = source.get("commit") if isinstance(source, dict) else None
    return value if isinstance(value, str) and is_full_sha(value) else None


def require_release_checkout() -> dict[str, str]:
    if command(("git", "branch", "--show-current")) != "main":
        raise UpdateError("blocked_contract_drift", "更新入口はmain branchのclean checkoutでだけ実行できます")
    dirty = command(("git", "status", "--porcelain", "--untracked-files=all"))
    if dirty:
        raise UpdateError("blocked_dirty_site", "サイトcheckoutに未commit変更があります")
    local = command(("git", "rev-parse", "HEAD"))
    remote = remote_sha("origin")
    if local != remote:
        raise UpdateError(
            "blocked_site_drift",
            f"サイトcheckoutがorigin/mainと一致しません: local={local}; remote={remote}",
        )
    try:
        command((sys.executable, "check_workflow.py"))
    except UpdateError as exc:
        raise UpdateError("blocked_contract_drift", str(exc)) from exc
    return {"site_local": local, "site_remote": remote}


def source_remote() -> str:
    return CONFIG["source_repository_url"].rstrip("/") + ".git"


def choose_source(requested: str | None) -> str:
    current = remote_sha(source_remote())
    if requested is not None:
        if not is_full_sha(requested):
            raise UpdateError("blocked_invalid_source", "--source-shaは小文字40桁のcommit SHAで指定してください")
        if requested != current:
            raise UpdateError(
                "blocked_source_drift",
                f"承認SHAと正本mainが一致しません: approved={requested}; remote={current}",
            )
    return current


def expected_run_title(request_id: str) -> str:
    try:
        parsed = uuid.UUID(request_id)
    except ValueError as exc:
        raise UpdateError("failed_run_correlation", f"request_idがUUIDではありません: {request_id}") from exc
    if str(parsed) != request_id:
        raise UpdateError("failed_run_correlation", f"request_idが正規UUID表現ではありません: {request_id}")
    return f"Pages / workflow_dispatch / {request_id}"


def match_request_run(
    runs: list[dict[str, Any]], request_id: str, expected_head_sha: str
) -> RunMatch | None:
    title = expected_run_title(request_id)
    matched = [run for run in runs if str(run.get("displayTitle", "")) == title]
    if len(matched) > 1:
        raise UpdateError("failed_run_correlation", f"request_idに一致するrunが複数あります: {request_id}")
    if not matched:
        return None
    run = matched[0]
    expected_metadata = {
        "event": "workflow_dispatch",
        "headBranch": "main",
        "headSha": expected_head_sha,
        "workflowName": WORKFLOW_NAME,
    }
    mismatched = {
        key: run.get(key)
        for key, expected in expected_metadata.items()
        if run.get(key) != expected
    }
    if mismatched:
        raise UpdateError(
            "failed_run_correlation",
            f"request_id一致runのmetadataが契約外です: {mismatched}",
        )
    database_id = run.get("databaseId")
    if not isinstance(database_id, int):
        raise UpdateError("failed_run_correlation", "対応runのdatabaseIdを取得できません")
    return RunMatch(database_id, str(run.get("displayTitle", "")), str(run.get("url", "")))


def dispatch(source_sha: str, check_external_links: bool, correlation_timeout: int) -> RunMatch:
    request_id = str(uuid.uuid4())
    expected_head_sha = command(("git", "rev-parse", "HEAD"))
    command(
        (
            "gh",
            "workflow",
            "run",
            WORKFLOW,
            "--ref",
            "main",
            "-f",
            f"source_sha={source_sha}",
            "-f",
            f"request_id={request_id}",
            "-f",
            f"check_external_links={'true' if check_external_links else 'false'}",
        )
    )
    deadline = time.monotonic() + correlation_timeout
    while time.monotonic() < deadline:
        raw = command(
            (
                "gh",
                "run",
                "list",
                "--workflow",
                WORKFLOW,
                "--event",
                "workflow_dispatch",
                "--limit",
                "30",
                "--json",
                "databaseId,displayTitle,event,headBranch,headSha,url,workflowName",
            )
        )
        try:
            runs = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UpdateError("failed_run_correlation", "Actions run一覧がJSONではありません") from exc
        if not isinstance(runs, list):
            raise UpdateError("failed_run_correlation", "Actions run一覧の形式が不正です")
        match = match_request_run(runs, request_id, expected_head_sha)
        if match:
            return match
        time.sleep(3)
    raise UpdateError("failed_run_correlation", f"起動したrunをrequest_idで特定できません: {request_id}")


def wait_for_run(run: RunMatch, timeout: int) -> None:
    try:
        subprocess.run(
            ("gh", "run", "watch", str(run.database_id), "--exit-status", "--interval", "10"),
            cwd=ROOT,
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("failed_run_timeout", f"Actions runが時間切れです: {run.url}") from exc
    except subprocess.CalledProcessError as exc:
        raise UpdateError("failed_workflow", f"Actions runが失敗しました: {run.url}") from exc


def verify_http_contract() -> None:
    base = CONFIG["base_url"].rstrip("/") + "/"
    request = Request(base, headers={"User-Agent": "ManabiGrid-Update-Runner/1.0"})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise UpdateError("failed_live_verify", f"トップがHTTP {response.status}です")
    missing = urllib.parse.urljoin(base, "__manabigrid_missing_page__")
    try:
        urlopen(Request(missing, headers={"User-Agent": "ManabiGrid-Update-Runner/1.0"}), timeout=20)
    except HTTPError as exc:
        if exc.code != 404:
            raise UpdateError("failed_live_verify", f"不存在URLがHTTP {exc.code}です") from exc
    else:
        raise UpdateError("failed_live_verify", "不存在URLがHTTP 404になりません")


def wait_for_live(expected: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if published_sha() == expected:
            verify_http_contract()
            return
        time.sleep(10)
    raise UpdateError("failed_live_verify", f"公開build-reportが期待SHAになりません: {expected}")


def write_report(payload: dict[str, Any]) -> None:
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def status_payload() -> dict[str, Any]:
    local = command(("git", "rev-parse", "HEAD"))
    site_remote = remote_sha("origin")
    source = remote_sha(source_remote())
    published = published_sha()
    return {
        "status": "current" if published == source else "update_available",
        "site": {"local": local, "remote": site_remote, "clean": not bool(command(("git", "status", "--porcelain")))},
        "source": {"remote": source, "published": published, "current": published == source},
    }


def publish(args: argparse.Namespace) -> dict[str, Any]:
    if not args.approve_publication:
        raise UpdateError(
            "blocked_missing_approval",
            "公開の明示承認を確認した実行者だけが--approve-publicationを指定できます",
        )
    checkout = require_release_checkout()
    expected = choose_source(args.source_sha)
    if published_sha() == expected:
        return {"status": "already_current", **checkout, "source_sha": expected, "runs": []}
    if args.dry_run:
        return {"status": "dry_run_ready", **checkout, "source_sha": expected, "runs": []}

    runs: list[dict[str, Any]] = []
    fixed_source = args.source_sha is not None
    for attempt in range(2):
        run = dispatch(expected, args.check_external_links, args.correlation_timeout)
        wait_for_run(run, args.run_timeout)
        wait_for_live(expected, args.live_timeout)
        runs.append({"database_id": run.database_id, "title": run.title, "url": run.url})
        latest = remote_sha(source_remote())
        if latest == expected:
            payload = {"status": "updated", **checkout, "source_sha": expected, "runs": runs}
            write_report(payload)
            return payload
        if fixed_source:
            payload = {
                "status": "blocked_source_drift_after_publish",
                **checkout,
                "source_sha": expected,
                "latest_source_sha": latest,
                "runs": runs,
            }
            write_report(payload)
            raise UpdateError(
                "blocked_source_drift_after_publish",
                "固定承認SHAの公開後に正本mainが進みました。新SHAは自動公開しません",
            )
        expected = latest
        if attempt == 0:
            continue
    raise UpdateError(
        "blocked_source_drift_after_publish",
        "正本mainが2回の公開中にも更新され続けたため、最新化を安全に打ち切りました",
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="正本・サイト・公開版のSHAを読み取り確認する")
    publish_parser = subparsers.add_parser("publish", help="承認済みの最新正本をPagesへ反映する")
    publish_parser.add_argument("--approve-publication", action="store_true")
    publish_parser.add_argument("--source-sha")
    publish_parser.add_argument("--check-external-links", action="store_true")
    publish_parser.add_argument("--dry-run", action="store_true")
    publish_parser.add_argument("--correlation-timeout", type=int, default=120)
    publish_parser.add_argument("--run-timeout", type=int, default=1800)
    publish_parser.add_argument("--live-timeout", type=int, default=600)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        payload = status_payload() if args.command == "status" else publish(args)
    except UpdateError as exc:
        payload = {"status": exc.status, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        payload = {"status": "failed_live_verify", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
