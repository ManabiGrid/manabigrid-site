#!/usr/bin/env python3
"""Dispatch and verify one exact ManabiGrid Pages update without dependencies."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
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
SCHEMA_VERSION = "4"
PUBLICATION_AUTHORITY = "not_observed"
SCHEDULE_STALE_HOURS = 72
OFFICIAL_SITE_REPOSITORY = "ManabiGrid/manabigrid-site"
OFFICIAL_BASE_URL = "https://manabigrid.github.io/manabigrid-site/"
OFFICIAL_SOURCE_REPOSITORY_URL = "https://github.com/ManabiGrid/manabigrid"


class UpdateError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RunMatch:
    database_id: int
    title: str
    url: str


def command_environment(arguments: Sequence[str]) -> dict[str, str]:
    environment = os.environ.copy()
    if arguments and arguments[0] == "gh":
        environment["GH_HOST"] = "github.com"
        environment.pop("GH_REPO", None)
    return environment


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
            env=command_environment(arguments),
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


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def remote_sha(remote: str) -> str:
    output = command(("git", "ls-remote", remote, "refs/heads/main"))
    value = output.split()[0] if output else ""
    if not is_full_sha(value):
        raise UpdateError("blocked_remote_state", f"mainのSHAを取得できません: {remote}")
    return value


def normalize_github_remote(value: str) -> str | None:
    remote = value.strip()
    scp_match = re.fullmatch(r"git@github\.com:(.+)", remote, re.IGNORECASE)
    if scp_match:
        path = scp_match.group(1)
    else:
        parsed = urllib.parse.urlsplit(remote)
        https_remote = (
            parsed.scheme.casefold() == "https"
            and parsed.hostname is not None
            and parsed.hostname.casefold() == "github.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
        )
        ssh_remote = (
            parsed.scheme.casefold() == "ssh"
            and parsed.hostname is not None
            and parsed.hostname.casefold() == "github.com"
            and parsed.username == "git"
            and parsed.password is None
            and parsed.port in {None, 22}
            and not parsed.query
            and not parsed.fragment
        )
        if not (https_remote or ssh_remote):
            return None
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        return None
    return path.casefold()


def expected_site_repository() -> str:
    return OFFICIAL_SITE_REPOSITORY.casefold()


def official_site_repository() -> str:
    configured = str(CONFIG.get("site_repository", ""))
    if configured.casefold() != expected_site_repository():
        raise UpdateError(
            "blocked_config_drift",
            "site.config.jsonのsite_repositoryが公式trust anchorと一致しません",
        )
    return OFFICIAL_SITE_REPOSITORY


def official_base_url() -> str:
    configured = str(CONFIG.get("base_url", "")).rstrip("/") + "/"
    if configured != OFFICIAL_BASE_URL:
        raise UpdateError(
            "blocked_config_drift",
            "site.config.jsonのbase_urlが公式trust anchorと一致しません",
        )
    return OFFICIAL_BASE_URL


def official_source_repository_url() -> str:
    configured = str(CONFIG.get("source_repository_url", "")).rstrip("/")
    if configured != OFFICIAL_SOURCE_REPOSITORY_URL:
        raise UpdateError(
            "blocked_config_drift",
            "site.config.jsonのsource_repository_urlが公式trust anchorと一致しません",
        )
    return OFFICIAL_SOURCE_REPOSITORY_URL


def site_origin() -> str:
    return command(("git", "remote", "get-url", "origin"))


def safe_site_origin(value: str) -> str:
    repository = normalize_github_remote(value)
    return (
        f"https://github.com/{repository}.git"
        if repository is not None
        else "unrecognized"
    )


def require_site_origin() -> str:
    official_site_repository()
    origin = site_origin()
    if normalize_github_remote(origin) != expected_site_repository():
        raise UpdateError(
            "blocked_site_origin",
            "site originがsite.config.jsonの公式repositoryと一致しません",
        )
    return safe_site_origin(origin)


def published_report() -> dict[str, Any] | None:
    base = official_base_url()
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


def published_site_sha() -> str | None:
    report = published_report()
    if not report:
        return None
    publication = report.get("publication")
    value = publication.get("site_commit") if isinstance(publication, dict) else None
    return value if isinstance(value, str) and is_full_sha(value) else None


def published_shas(report: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not report:
        return None, None
    source = report.get("source")
    publication = report.get("publication")
    source_sha = source.get("commit") if isinstance(source, dict) else None
    site_sha = (
        publication.get("site_commit")
        if isinstance(publication, dict)
        else None
    )
    return (
        source_sha
        if isinstance(source_sha, str) and is_full_sha(source_sha)
        else None,
        site_sha if isinstance(site_sha, str) and is_full_sha(site_sha) else None,
    )


def workflow_operational_status(
    expected_site_sha: str | None = None,
) -> dict[str, Any]:
    if expected_site_sha is None or not is_full_sha(expected_site_sha):
        return {
            "status": "unknown",
            "workflow_state": "unknown",
            "latest_schedule": None,
            "error": "current site main SHA is unavailable",
        }
    try:
        repository = official_site_repository()
        workflow_raw = command(
            (
                "gh",
                "api",
                f"repos/{repository}/actions/workflows/{WORKFLOW}",
            )
        )
        workflow = json.loads(workflow_raw)
        if not isinstance(workflow, dict):
            raise ValueError("workflow response is not an object")
        state = str(workflow.get("state", ""))
        if state != "active":
            return {
                "status": "disabled",
                "workflow_state": state or "unknown",
                "latest_schedule": None,
            }

        runs_raw = command(
            (
                "gh",
                "run",
                "list",
                "--repo",
                repository,
                "--workflow",
                WORKFLOW,
                "--event",
                "schedule",
                "--limit",
                "1",
                "--json",
                "createdAt,conclusion,status,url,headSha,headBranch",
            )
        )
        runs = json.loads(runs_raw)
        if not isinstance(runs, list):
            raise ValueError("schedule run response is not an array")
        if not runs:
            return {
                "status": "never_run",
                "workflow_state": state,
                "latest_schedule": None,
            }
        latest = runs[0]
        if not isinstance(latest, dict):
            raise ValueError("latest schedule run is not an object")
        created_at = str(latest.get("createdAt", ""))
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_hours = (
            datetime.now(timezone.utc) - created.astimezone(timezone.utc)
        ).total_seconds() / 3600
        conclusion = latest.get("conclusion")
        run_status = latest.get("status")
        head_sha = latest.get("headSha")
        head_branch = latest.get("headBranch")
        if age_hours > SCHEDULE_STALE_HOURS:
            status = "stale"
        elif head_sha != expected_site_sha or head_branch != "main":
            status = "unverified_revision"
        elif run_status != "completed":
            status = "in_progress"
        elif conclusion != "success":
            status = "failed"
        else:
            status = "active"
        return {
            "status": status,
            "workflow_state": state,
            "latest_schedule": {
                "created_at": created_at,
                "age_hours": round(age_hours, 2),
                "status": run_status,
                "conclusion": conclusion,
                "head_sha": head_sha,
                "head_branch": head_branch,
                "head_matches_current": head_sha == expected_site_sha,
                "branch_matches_main": head_branch == "main",
                "url": latest.get("url"),
            },
        }
    except (
        UpdateError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        return {
            "status": "unknown",
            "workflow_state": "unknown",
            "latest_schedule": None,
            "error": str(exc),
        }


def require_release_checkout() -> dict[str, str]:
    origin = require_site_origin()
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
    return {"site_local": local, "site_remote": remote, "site_origin": origin}


def source_remote() -> str:
    return official_source_repository_url() + ".git"


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


def match_push_run(
    runs: list[dict[str, Any]], expected_head_sha: str
) -> RunMatch | None:
    if not is_full_sha(expected_head_sha):
        raise UpdateError("failed_run_correlation", "site commit SHAが不正です")
    title = f"Pages / push / {expected_head_sha}"
    matched = [run for run in runs if str(run.get("displayTitle", "")) == title]
    if len(matched) > 1:
        raise UpdateError(
            "failed_run_correlation",
            f"site commitに一致するpush runが複数あります: {expected_head_sha}",
        )
    if not matched:
        return None
    run = matched[0]
    expected_metadata = {
        "event": "push",
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
            f"site commit一致runのmetadataが契約外です: {mismatched}",
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
            "--repo",
            official_site_repository(),
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
                "--repo",
                official_site_repository(),
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


def find_push_run(expected_head_sha: str, correlation_timeout: int) -> RunMatch:
    deadline = time.monotonic() + correlation_timeout
    while time.monotonic() < deadline:
        raw = command(
            (
                "gh",
                "run",
                "list",
                "--repo",
                official_site_repository(),
                "--workflow",
                WORKFLOW,
                "--event",
                "push",
                "--branch",
                "main",
                "--commit",
                expected_head_sha,
                "--limit",
                "20",
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
        match = match_push_run(runs, expected_head_sha)
        if match:
            return match
        time.sleep(3)
    raise UpdateError(
        "failed_run_correlation",
        f"site commitに対応するpush runを特定できません: {expected_head_sha}",
    )


def wait_for_run(run: RunMatch, timeout: int) -> None:
    try:
        subprocess.run(
            (
                "gh",
                "run",
                "watch",
                str(run.database_id),
                "--repo",
                official_site_repository(),
                "--exit-status",
                "--interval",
                "10",
            ),
            cwd=ROOT,
            check=True,
            timeout=timeout,
            env=command_environment(("gh",)),
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("failed_run_timeout", f"Actions runが時間切れです: {run.url}") from exc
    except subprocess.CalledProcessError as exc:
        raise UpdateError("failed_workflow", f"Actions runが失敗しました: {run.url}") from exc


def verify_http_contract() -> None:
    base = official_base_url()
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


def verify_pages_deployment(expected_site_sha: str) -> None:
    raw = command(
        (
            "gh",
            "api",
            (
                f"repos/{official_site_repository()}/pages/deployments/"
                f"{expected_site_sha}"
            ),
        )
    )
    try:
        deployment = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateError("failed_live_verify", "Pages deployment statusがJSONではありません") from exc
    if not isinstance(deployment, dict) or deployment.get("status") != "succeed":
        raise UpdateError(
            "failed_live_verify",
            f"Pages deploymentが成功状態ではありません: {deployment}",
        )


def wait_for_site_live(
    expected_site_sha: str, expected_source_sha: str, timeout: int
) -> None:
    deadline = time.monotonic() + timeout
    last_error = "公開build-reportをまだ取得できません"
    while time.monotonic() < deadline:
        report = published_report()
        if report:
            source = report.get("source")
            publication = report.get("publication")
            published_source = source.get("commit") if isinstance(source, dict) else None
            published_site = (
                publication.get("site_commit")
                if isinstance(publication, dict)
                else None
            )
            if (
                published_site == expected_site_sha
                and published_source == expected_source_sha
            ):
                verify_http_contract()
                return
            last_error = (
                f"published_site={published_site}; expected_site={expected_site_sha}; "
                f"published_source={published_source}; expected_source={expected_source_sha}"
            )
        time.sleep(10)
    raise UpdateError("failed_live_verify", last_error)


def write_report(payload: dict[str, Any]) -> None:
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def publish_meta(status: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "schema_version": SCHEMA_VERSION,
        "publication_authority": PUBLICATION_AUTHORITY,
        "generated_at": utc_now(),
        **payload,
    }


def persist_snapshot(payload: dict[str, Any]) -> None:
    try:
        write_report(payload)
    except OSError as exc:
        payload["report_persist_error"] = f"update-report.jsonへ保存できません: {exc}"


def status_payload() -> dict[str, Any]:
    configuration_error: UpdateError | None = None
    try:
        official_site_repository()
        official_base_url()
        official_source_repository_url()
    except UpdateError as exc:
        configuration_error = exc

    local = command(("git", "rev-parse", "HEAD"))
    origin = site_origin()
    origin_matches = (
        normalize_github_remote(origin) == expected_site_repository()
    )
    site_remote = remote_sha("origin") if origin_matches else None
    source = remote_sha(OFFICIAL_SOURCE_REPOSITORY_URL + ".git")
    report = published_report() if configuration_error is None else None
    published_source, published_site = published_shas(report)
    published_state = (
        "observed"
        if report is not None
        and published_source is not None
        and published_site is not None
        else "unknown"
    )
    operational = (
        workflow_operational_status(site_remote)
        if configuration_error is None
        else {
            "status": "unknown",
            "workflow_state": "unknown",
            "latest_schedule": None,
            "error": str(configuration_error),
        }
    )

    site_clean = not bool(command(("git", "status", "--porcelain", "--untracked-files=all")))
    current_branch = command(("git", "branch", "--show-current"))
    workflow_contract_pass = True
    try:
        command((sys.executable, "check_workflow.py"))
    except UpdateError:
        workflow_contract_pass = False

    source_sync = (
        "unknown"
        if published_state == "unknown"
        else "current"
        if published_source == source
        else "update_available"
    )
    site_sync = (
        "unknown"
        if published_state == "unknown"
        else "current"
        if published_site == local
        else "site_release_pending"
    )

    if configuration_error is not None:
        release_readiness = "blocked_config_drift"
    elif not origin_matches:
        release_readiness = "blocked_site_origin"
    elif not site_clean:
        release_readiness = "blocked_dirty_site"
    elif current_branch != "main":
        release_readiness = "blocked_contract_drift"
    elif local != site_remote:
        release_readiness = "blocked_site_drift"
    elif not workflow_contract_pass:
        release_readiness = "blocked_contract_drift"
    elif published_state == "unknown":
        release_readiness = "blocked_published_state_unknown"
    else:
        release_readiness = "ready"

    operational_status = str(operational.get("status", "unknown"))
    if release_readiness != "ready":
        status = release_readiness
    elif site_sync != "current":
        status = site_sync
    elif operational_status != "active":
        status = f"blocked_schedule_{operational_status}"
    else:
        status = source_sync

    if release_readiness == "ready" and site_sync == "site_release_pending":
        next_action_code = "verify_site_release"
    elif release_readiness == "ready" and operational_status != "active":
        next_action_code = "inspect_schedule_health"
    else:
        next_action_code = {
            "ready": (
                "await_publication_approval"
                if source_sync == "update_available"
                else "monitor"
            ),
            "blocked_site_origin": "inspect_site_origin",
            "blocked_dirty_site": "preserve_and_inspect_dirty_worktree",
            "blocked_site_drift": "inspect_site_drift",
            "blocked_contract_drift": "inspect_contract_drift",
            "blocked_published_state_unknown": "inspect_published_state",
            "blocked_config_drift": "inspect_config_drift",
        }.get(release_readiness, "investigate")

    return publish_meta(
        status,
        {
            "source_sync": source_sync,
            "site_sync": site_sync,
            "published_state": published_state,
            "configuration": {
                "valid": configuration_error is None,
                "error": str(configuration_error) if configuration_error else None,
            },
            "release_readiness": release_readiness,
            "operational_readiness": operational_status,
            "next_action_code": next_action_code,
            "site": {
                "local": local,
                "remote": site_remote,
                "published": published_site,
                "origin": safe_site_origin(origin),
                "origin_matches_config": origin_matches,
                "clean": site_clean,
                "branch": current_branch,
                "local_eq_remote": local == site_remote,
                "workflow_contract_pass": workflow_contract_pass,
            },
            "source": {
                "remote": source,
                "published": published_source,
                "current": source_sync == "current",
            },
            "operations": operational,
        },
    )


def publish(args: argparse.Namespace) -> dict[str, Any]:
    if not args.approve_publication:
        raise UpdateError(
            "blocked_missing_approval",
            "公開の明示承認を確認した実行者だけが--approve-publicationを指定できます",
        )
    checkout = require_release_checkout()
    expected = choose_source(args.source_sha)
    published_source, published_site = published_shas(published_report())
    if published_source == expected:
        if published_site != checkout["site_local"]:
            raise UpdateError(
                "blocked_site_release_requires_verification",
                "正本SHAは一致しますがsite commitが公開版と不一致です。"
                "verify-site-releaseで対象push runを照合してください",
            )
        return publish_meta(
            "already_current",
            {"runs": [], **checkout, "source_sha": expected},
        )
    if args.dry_run:
        return publish_meta("dry_run_ready", {"runs": [], **checkout, "source_sha": expected})

    runs: list[dict[str, Any]] = []
    fixed_source = args.source_sha is not None
    for attempt in range(2):
        run = dispatch(expected, args.check_external_links, args.correlation_timeout)
        wait_for_run(run, args.run_timeout)
        wait_for_site_live(
            checkout["site_local"],
            expected,
            args.live_timeout,
        )
        verify_pages_deployment(checkout["site_local"])
        runs.append({"database_id": run.database_id, "title": run.title, "url": run.url})
        latest = remote_sha(source_remote())
        if latest == expected:
            payload = publish_meta(
                "updated",
                {
                    "runs": runs,
                    **checkout,
                    "source_sha": expected,
                },
            )
            write_report(payload)
            return payload
        if fixed_source:
            payload = publish_meta(
                "blocked_source_drift_after_publish",
                {
                    "runs": runs,
                    **checkout,
                    "source_sha": expected,
                    "latest_source_sha": latest,
                },
            )
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


def verify_site_release(args: argparse.Namespace) -> dict[str, Any]:
    checkout = require_release_checkout()
    expected_site = args.site_sha or checkout["site_local"]
    if not is_full_sha(expected_site) or expected_site != checkout["site_local"]:
        raise UpdateError(
            "blocked_site_drift",
            "検証対象site SHAがcleanな現在HEADと一致しません",
        )
    expected_source = choose_source(args.source_sha)
    run = find_push_run(expected_site, args.correlation_timeout)
    wait_for_run(run, args.run_timeout)
    wait_for_site_live(expected_site, expected_source, args.live_timeout)
    verify_pages_deployment(expected_site)
    payload = publish_meta(
        "site_release_verified",
        {
            "runs": [
                {"database_id": run.database_id, "title": run.title, "url": run.url}
            ],
            **checkout,
            "site_sha": expected_site,
            "source_sha": expected_source,
        },
    )
    write_report(payload)
    return payload


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
    verify_parser = subparsers.add_parser(
        "verify-site-release",
        help="push済みsite commitの該当run・Pages・公開build-reportを完全一致で照合する",
    )
    verify_parser.add_argument("--site-sha")
    verify_parser.add_argument("--source-sha")
    verify_parser.add_argument("--correlation-timeout", type=int, default=120)
    verify_parser.add_argument("--run-timeout", type=int, default=1800)
    verify_parser.add_argument("--live-timeout", type=int, default=600)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            payload = status_payload()
        elif args.command == "publish":
            payload = publish(args)
        else:
            payload = verify_site_release(args)
    except UpdateError as exc:
        payload = publish_meta(exc.status, {"error": str(exc)})
        persist_snapshot(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        payload = publish_meta("failed_live_verify", {"error": str(exc)})
        persist_snapshot(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    if args.command == "status":
        persist_snapshot(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
