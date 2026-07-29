#!/usr/bin/env python3
"""Verify that a Pages commit is official main from one green reviewed PR."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPOSITORY = "ManabiGrid/manabigrid-site"
REMOTE_URL = "https://github.com/ManabiGrid/manabigrid-site.git"
MAIN_REF = "refs/heads/main"
API_ROOT = "https://api.github.com"
REQUIRED_CHECK_NAME = "manabigrid-site-pr-gate"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ProvenanceError(RuntimeError):
    """A safe release provenance failure with no external response body."""


def read_remote_main_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "ls-remote", REMOTE_URL, MAIN_REF],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            check=False,
        )
    except OSError as exc:
        raise ProvenanceError(
            "official site main could not be resolved"
        ) from exc
    if completed.returncode != 0:
        raise ProvenanceError("official site main could not be resolved")
    fields = completed.stdout.split()
    if (
        len(fields) != 2
        or not SHA_PATTERN.fullmatch(fields[0])
        or fields[1] != MAIN_REF
    ):
        raise ProvenanceError(
            "official site main did not resolve to one exact commit"
        )
    return fields[0]


def github_api(path: str, token: str) -> Any:
    if not token:
        raise ProvenanceError("GitHub token is unavailable")
    request = Request(
        API_ROOT + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ManabiGrid-Pages-Provenance/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read(5_000_001)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise ProvenanceError("GitHub provenance API failed") from exc
    if len(body) > 5_000_000:
        raise ProvenanceError("GitHub provenance response was too large")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(
            "GitHub provenance response was malformed"
        ) from exc


def select_merged_pull_request(
    payload: object,
    site_sha: str,
) -> tuple[int, str]:
    if not isinstance(payload, list):
        raise ProvenanceError("associated pull request data was malformed")
    matches: list[tuple[int, str]] = []
    for pull_request in payload:
        if not isinstance(pull_request, dict):
            raise ProvenanceError(
                "associated pull request data was malformed"
            )
        base = pull_request.get("base")
        head = pull_request.get("head")
        if (
            pull_request.get("merge_commit_sha") != site_sha
            or pull_request.get("state") != "closed"
            or not pull_request.get("merged_at")
            or not isinstance(base, dict)
            or base.get("ref") != "main"
        ):
            continue
        number = pull_request.get("number")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            or not isinstance(head_sha, str)
            or not SHA_PATTERN.fullmatch(head_sha)
        ):
            raise ProvenanceError(
                "associated pull request identity was malformed"
            )
        matches.append((number, head_sha))
    if len(matches) != 1:
        raise ProvenanceError(
            "site commit is not bound to exactly one merged main PR"
        )
    return matches[0]


def validate_required_check(payload: object, head_sha: str) -> None:
    if not isinstance(payload, dict):
        raise ProvenanceError("PR check data was malformed")
    total_count = payload.get("total_count")
    checks = payload.get("check_runs")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or not isinstance(checks, list)
        or total_count != len(checks)
    ):
        raise ProvenanceError("PR check result set was incomplete")
    required = [
        check
        for check in checks
        if isinstance(check, dict)
        and check.get("name") == REQUIRED_CHECK_NAME
    ]
    if len(required) != 1:
        raise ProvenanceError("required PR gate was missing or duplicated")
    check = required[0]
    app = check.get("app")
    if (
        check.get("head_sha") != head_sha
        or check.get("status") != "completed"
        or check.get("conclusion") != "success"
        or not isinstance(app, dict)
        or app.get("slug") != "github-actions"
    ):
        raise ProvenanceError(
            "required PR gate is not a successful GitHub Actions check"
        )


def validate_merge_parents(
    payload: object,
    site_sha: str,
    head_sha: str,
) -> None:
    if not isinstance(payload, dict):
        raise ProvenanceError("site commit data was malformed")
    parents = payload.get("parents")
    if payload.get("sha") != site_sha or not isinstance(parents, list):
        raise ProvenanceError("site commit data was malformed")
    parent_shas = [
        parent.get("sha") if isinstance(parent, dict) else None
        for parent in parents
    ]
    if (
        len(parent_shas) != 2
        or any(
            not isinstance(parent, str)
            or not SHA_PATTERN.fullmatch(parent)
            for parent in parent_shas
        )
        or parent_shas[0] == parent_shas[1]
        or parent_shas[1] != head_sha
    ):
        raise ProvenanceError(
            "site commit is not a two-parent merge of the reviewed PR head"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-sha", required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args(argv)
    try:
        if args.ref != MAIN_REF:
            raise ProvenanceError("Pages may run only from refs/heads/main")
        if not SHA_PATTERN.fullmatch(args.site_sha):
            raise ProvenanceError(
                "site SHA must be one lowercase 40-character commit"
            )
        if read_remote_main_sha() != args.site_sha:
            raise ProvenanceError(
                "Pages commit does not match official remote main"
            )
        token = os.environ.get("GITHUB_TOKEN", "")
        pull_requests = github_api(
            f"/repos/{REPOSITORY}/commits/{args.site_sha}/pulls",
            token,
        )
        number, head_sha = select_merged_pull_request(
            pull_requests,
            args.site_sha,
        )
        site_commit = github_api(
            f"/repos/{REPOSITORY}/commits/{args.site_sha}",
            token,
        )
        validate_merge_parents(
            site_commit,
            args.site_sha,
            head_sha,
        )
        checks = github_api(
            (
                f"/repos/{REPOSITORY}/commits/{head_sha}/check-runs"
                "?per_page=100"
            ),
            token,
        )
        validate_required_check(checks, head_sha)
    except ProvenanceError as exc:
        print(f"Release provenance check: FAIL\n- {exc}")
        return 1
    print(
        "Release provenance check: PASS "
        f"(merged_pr={number}; required_gate=success)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
