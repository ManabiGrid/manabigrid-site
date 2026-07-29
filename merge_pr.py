#!/usr/bin/env python3
"""Merge one reviewed PR with a verified noreply author and a locked head SHA."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass

import check_commit_identity


ROOT = check_commit_identity.ROOT
REPOSITORY = "ManabiGrid/manabigrid-site"
REQUIRED_CHECK_NAME = "manabigrid-site-pr-gate"
REQUIRED_WORKFLOW_NAME = "Validate ManabiGrid site code"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
LOGIN_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"
)


class MergeGuardError(RuntimeError):
    """A merge precondition failed without exposing sensitive values."""


@dataclass(frozen=True)
class AuthenticatedUser:
    database_id: int
    login: str

    @property
    def current_noreply(self) -> str:
        return (
            f"{self.database_id}+{self.login}@users.noreply.github.com"
        )

    @property
    def legacy_noreply(self) -> str:
        return f"{self.login}@users.noreply.github.com"


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    base_ref: str
    state: str
    is_draft: bool
    url: str


def safe_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GH_HOST"] = "github.com"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment.pop("GH_REPO", None)
    return environment


def run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            cwd=ROOT,
            env=safe_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise MergeGuardError("required local command could not run") from exc


def require_success(
    completed: subprocess.CompletedProcess[str],
    purpose: str,
) -> str:
    if completed.returncode == 0:
        return completed.stdout
    raise MergeGuardError(
        f"{purpose} failed with exit code {completed.returncode}"
    )


def load_authenticated_user() -> AuthenticatedUser:
    completed = run_command(
        ["gh", "api", "user", "--jq", "{id: .id, login: .login}"]
    )
    raw = require_success(completed, "GitHub account verification")
    try:
        payload = json.loads(raw)
        database_id = payload["id"]
        login = payload["login"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MergeGuardError(
            "GitHub account response was malformed"
        ) from exc
    if (
        isinstance(database_id, bool)
        or not isinstance(database_id, int)
        or database_id <= 0
        or not isinstance(login, str)
        or not LOGIN_PATTERN.fullmatch(login)
    ):
        raise MergeGuardError("GitHub account identity was invalid")
    return AuthenticatedUser(database_id=database_id, login=login)


def verify_pages_environment_policy() -> None:
    environment_result = run_command(
        [
            "gh",
            "api",
            f"repos/{REPOSITORY}/environments/github-pages",
        ]
    )
    raw_environment = require_success(
        environment_result,
        "Pages environment verification",
    )
    branch_result = run_command(
        [
            "gh",
            "api",
            (
                f"repos/{REPOSITORY}/environments/github-pages/"
                "deployment-branch-policies"
            ),
        ]
    )
    raw_branches = require_success(
        branch_result,
        "Pages branch policy verification",
    )
    try:
        environment = json.loads(raw_environment)
        deployment_policy = environment["deployment_branch_policy"]
        branches = json.loads(raw_branches)
        branch_policies = branches["branch_policies"]
        total_count = branches["total_count"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise MergeGuardError(
            "Pages environment policy response was malformed"
        ) from exc
    if (
        not isinstance(deployment_policy, dict)
        or deployment_policy.get("protected_branches") is not False
        or deployment_policy.get("custom_branch_policies") is not True
        or total_count != 1
        or not isinstance(branch_policies, list)
        or len(branch_policies) != 1
        or not isinstance(branch_policies[0], dict)
        or branch_policies[0].get("name") != "main"
        or branch_policies[0].get("type") != "branch"
    ):
        raise MergeGuardError(
            "Pages environment is not restricted to the main branch"
        )


def load_local_author_email(user: AuthenticatedUser) -> str:
    completed = run_command(
        ["git", "config", "--local", "--get", "user.email"]
    )
    configured = require_success(
        completed,
        "local Git author verification",
    ).strip()
    allowed = {
        user.current_noreply.casefold(),
        user.legacy_noreply.casefold(),
    }
    if configured.casefold() not in allowed:
        raise MergeGuardError(
            "local Git author is not the authenticated account's "
            "verified noreply identity"
        )
    return configured


def load_pull_request(reference: str) -> PullRequest:
    completed = run_command(
        [
            "gh",
            "pr",
            "view",
            reference,
            "--repo",
            REPOSITORY,
            "--json",
            (
                "number,headRefOid,baseRefName,state,isDraft,url,"
                "mergeable,mergeStateStatus,statusCheckRollup"
            ),
        ]
    )
    raw = require_success(completed, "pull request verification")
    try:
        payload = json.loads(raw)
        pull_request = PullRequest(
            number=payload["number"],
            head_sha=payload["headRefOid"],
            base_ref=payload["baseRefName"],
            state=payload["state"],
            is_draft=payload["isDraft"],
            url=payload["url"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MergeGuardError(
            "pull request response was malformed"
        ) from exc
    if (
        isinstance(pull_request.number, bool)
        or not isinstance(pull_request.number, int)
        or pull_request.number <= 0
    ):
        raise MergeGuardError("pull request number was invalid")
    if not isinstance(
        pull_request.head_sha,
        str,
    ) or not SHA_PATTERN.fullmatch(pull_request.head_sha):
        raise MergeGuardError("pull request head SHA was invalid")
    if pull_request.base_ref != "main":
        raise MergeGuardError("pull request base is not main")
    if pull_request.state != "OPEN":
        raise MergeGuardError("pull request is not open")
    if pull_request.is_draft:
        raise MergeGuardError("pull request is still a draft")
    if not isinstance(pull_request.url, str) or not pull_request.url.startswith(
        f"https://github.com/{REPOSITORY}/pull/"
    ):
        raise MergeGuardError("pull request URL was outside the fixed repository")
    if payload.get("mergeable") != "MERGEABLE":
        raise MergeGuardError("pull request is not currently mergeable")
    if payload.get("mergeStateStatus") != "CLEAN":
        raise MergeGuardError("pull request merge state is not clean")
    checks = payload.get("statusCheckRollup")
    if not isinstance(checks, list):
        raise MergeGuardError("pull request check results were malformed")
    required_checks = [
        check
        for check in checks
        if isinstance(check, dict)
        and check.get("name") == REQUIRED_CHECK_NAME
    ]
    if len(required_checks) != 1:
        raise MergeGuardError(
            "the required PR gate was missing or duplicated"
        )
    required_check = required_checks[0]
    if (
        required_check.get("workflowName") != REQUIRED_WORKFLOW_NAME
        or required_check.get("status") != "COMPLETED"
        or required_check.get("conclusion") != "SUCCESS"
    ):
        raise MergeGuardError(
            "the required PR gate has not completed successfully"
        )
    return pull_request


def validate_pull_request_commits(pull_request: PullRequest) -> int:
    completed = run_command(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            (
                f"repos/{REPOSITORY}/pulls/{pull_request.number}/commits"
                "?per_page=100"
            ),
            "--jq",
            (
                "map(.[]) | map({sha: .sha, "
                "author: .commit.author.email, "
                "committer: .commit.committer.email})"
            ),
        ]
    )
    raw = require_success(completed, "pull request commit verification")
    try:
        commits = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MergeGuardError(
            "pull request commit response was malformed"
        ) from exc
    if not isinstance(commits, list) or not commits:
        raise MergeGuardError("pull request has no verifiable commits")
    for index, commit in enumerate(commits, start=1):
        if not isinstance(commit, dict):
            raise MergeGuardError("pull request commit metadata was malformed")
        sha = commit.get("sha")
        author = commit.get("author")
        committer = commit.get("committer")
        if (
            not isinstance(sha, str)
            or not SHA_PATTERN.fullmatch(sha)
            or not isinstance(author, str)
            or not isinstance(committer, str)
        ):
            raise MergeGuardError(
                "pull request commit metadata was malformed"
            )
        try:
            check_commit_identity.validate_commit_identity(author, committer)
        except check_commit_identity.IdentityCheckError as exc:
            raise MergeGuardError(
                f"pull request commit {index} failed identity policy: {exc}"
            ) from None
    if commits[-1]["sha"] != pull_request.head_sha:
        raise MergeGuardError(
            "pull request commit list did not end at the reviewed head SHA"
        )
    return len(commits)


def build_merge_command(
    reference: str,
    author_email: str,
    head_sha: str,
) -> list[str]:
    if not SHA_PATTERN.fullmatch(head_sha):
        raise MergeGuardError("pull request head SHA was invalid")
    return [
        "gh",
        "pr",
        "merge",
        reference,
        "--repo",
        REPOSITORY,
        "--merge",
        "--author-email",
        author_email,
        "--match-head-commit",
        head_sha,
    ]


def verify_merged_result(reference: str, expected_head: str) -> str:
    completed = run_command(
        [
            "gh",
            "pr",
            "view",
            reference,
            "--repo",
            REPOSITORY,
            "--json",
            "state,headRefOid,mergeCommit,url",
        ]
    )
    raw = require_success(completed, "post-merge PR verification")
    try:
        payload = json.loads(raw)
        merge_commit = payload["mergeCommit"]
        merge_sha = merge_commit["oid"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise MergeGuardError("post-merge response was malformed") from exc
    if payload.get("state") != "MERGED":
        raise MergeGuardError(
            "PR did not reach MERGED; queued or pending is not success"
        )
    if payload.get("headRefOid") != expected_head:
        raise MergeGuardError("merged PR head changed after review")
    if not isinstance(merge_sha, str) or not SHA_PATTERN.fullmatch(merge_sha):
        raise MergeGuardError("merge commit SHA was invalid")
    return merge_sha


def verify_remote_merge_commit(
    merge_sha: str,
    expected_head: str,
) -> None:
    completed = run_command(
        [
            "gh",
            "api",
            f"repos/{REPOSITORY}/commits/{merge_sha}",
            "--jq",
            (
                "{sha: .sha, author: .commit.author.email, "
                "committer: .commit.committer.email, "
                "parents: [.parents[].sha]}"
            ),
        ]
    )
    raw = require_success(completed, "remote merge commit verification")
    try:
        payload = json.loads(raw)
        merge_result_sha = payload["sha"]
        author = payload["author"]
        committer = payload["committer"]
        parents = payload["parents"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise MergeGuardError(
            "remote merge commit response was malformed"
        ) from exc
    if merge_result_sha != merge_sha:
        raise MergeGuardError("remote merge commit SHA did not match")
    if (
        not isinstance(parents, list)
        or len(parents) != 2
        or parents[1] != expected_head
        or parents[0] == parents[1]
        or any(
            not isinstance(parent, str)
            or not SHA_PATTERN.fullmatch(parent)
            for parent in parents
        )
    ):
        raise MergeGuardError(
            "remote merge commit is not bound to the reviewed PR head"
        )
    if not isinstance(author, str) or not isinstance(committer, str):
        raise MergeGuardError("remote merge identity was malformed")
    try:
        check_commit_identity.validate_commit_identity(author, committer)
    except check_commit_identity.IdentityCheckError as exc:
        raise MergeGuardError(
            f"remote merge commit failed identity policy: {exc}"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pull_request")
    parser.add_argument(
        "--approve-merge",
        action="store_true",
        help="technical guard; valid only with explicit user approval",
    )
    parser.add_argument(
        "--reviewed-head-sha",
        help="exact 40-character PR head covered by independent review",
    )
    args = parser.parse_args(argv)
    if not args.approve_merge:
        print(
            "PR merge guard: STOPPED\n"
            "- explicit merge authorization and --approve-merge are required"
        )
        return 2
    try:
        user = load_authenticated_user()
        verify_pages_environment_policy()
        author_email = load_local_author_email(user)
        pull_request = load_pull_request(args.pull_request)
        if (
            not isinstance(args.reviewed_head_sha, str)
            or not SHA_PATTERN.fullmatch(args.reviewed_head_sha)
            or args.reviewed_head_sha != pull_request.head_sha
        ):
            raise MergeGuardError(
                "independent review SHA does not match the live PR head"
            )
        validate_pull_request_commits(pull_request)
        merge_attempt = run_command(
            build_merge_command(
                args.pull_request,
                author_email,
                pull_request.head_sha,
            )
        )
    except MergeGuardError as exc:
        print(f"PR merge guard: FAIL\n- {exc}")
        return 1
    try:
        merge_sha = verify_merged_result(
            args.pull_request,
            pull_request.head_sha,
        )
        verify_remote_merge_commit(
            merge_sha,
            pull_request.head_sha,
        )
    except MergeGuardError as exc:
        print(
            "PR merge guard: STATE UNKNOWN AFTER ATTEMPT\n"
            "- merge may already have occurred; do not retry\n"
            f"- post-attempt verification failed: {exc}"
        )
        return 3
    if merge_attempt.returncode != 0:
        print(
            "PR merge guard: PASS "
            "(server merge verified after an ambiguous command result)"
        )
        return 0
    print(
        "PR merge guard: PASS "
        "(verified noreply author; locked reviewed head SHA)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
