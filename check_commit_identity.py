#!/usr/bin/env python3
"""Reject a publication commit that could expose a non-noreply email address."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
GITHUB_USER_NOREPLY_PATTERN = re.compile(
    r"^(?:[0-9]+\+)?[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"@users\.noreply\.github\.com$",
    re.IGNORECASE,
)
GITHUB_SERVER_COMMITTERS = frozenset({"noreply@github.com"})


class IdentityCheckError(RuntimeError):
    """A safe, non-sensitive commit identity validation failure."""


def is_github_user_noreply(value: str) -> bool:
    return bool(GITHUB_USER_NOREPLY_PATTERN.fullmatch(value))


def is_allowed_committer_noreply(value: str) -> bool:
    return (
        is_github_user_noreply(value)
        or value.casefold() in GITHUB_SERVER_COMMITTERS
    )


def read_commit_identity(repo: Path, commit: str) -> tuple[str, str]:
    if not SHA_PATTERN.fullmatch(commit):
        raise IdentityCheckError(
            "commit must be one lowercase 40-character SHA"
        )
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        object_type = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-t", commit],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        if object_type.returncode != 0 or object_type.stdout.strip() != "commit":
            raise IdentityCheckError("target SHA is not a readable commit")
        completed = subprocess.run(
            ["git", "-C", str(repo), "show", "-s", "--format=%ae%n%ce", commit],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
    except OSError as exc:
        raise IdentityCheckError("git could not read commit metadata") from exc
    if completed.returncode != 0:
        raise IdentityCheckError("git could not read commit metadata")
    lines = completed.stdout.splitlines()
    if len(lines) != 2 or not all(lines):
        raise IdentityCheckError("commit identity metadata is malformed")
    return lines[0], lines[1]


def list_commits_after(
    repo: Path,
    baseline: str,
    target: str,
) -> list[str]:
    if not SHA_PATTERN.fullmatch(baseline):
        raise IdentityCheckError(
            "baseline must be one lowercase 40-character SHA"
        )
    if not SHA_PATTERN.fullmatch(target):
        raise IdentityCheckError(
            "commit must be one lowercase 40-character SHA"
        )
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                baseline,
                target,
            ],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        if ancestry.returncode != 0:
            raise IdentityCheckError(
                "privacy baseline is not an ancestor of the target commit"
            )
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-list",
                "--reverse",
                f"{baseline}..{target}",
            ],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
    except OSError as exc:
        raise IdentityCheckError("git could not read commit history") from exc
    if completed.returncode != 0:
        raise IdentityCheckError("git could not read commit history")
    commits = completed.stdout.splitlines()
    if not commits:
        raise IdentityCheckError(
            "no commit exists after the fixed privacy baseline"
        )
    if any(not SHA_PATTERN.fullmatch(commit) for commit in commits):
        raise IdentityCheckError("commit history contained a malformed SHA")
    return commits


def validate_commit_identity(author: str, committer: str) -> None:
    if not is_github_user_noreply(author):
        raise IdentityCheckError("author email is not a GitHub noreply identity")
    if not is_allowed_committer_noreply(committer):
        raise IdentityCheckError(
            "committer email is not an allowed GitHub noreply identity"
        )


def validate_commit_range(
    repo: Path,
    baseline: str,
    target: str,
) -> int:
    commits = list_commits_after(repo, baseline, target)
    for commit in commits:
        author, committer = read_commit_identity(repo, commit)
        validate_commit_identity(author, committer)
    return len(commits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument(
        "--since",
        help="exclusive fixed baseline; inspect every reachable commit after it",
    )
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        repo = args.repo.resolve()
        if args.since:
            checked = validate_commit_range(repo, args.since, args.commit)
        else:
            author, committer = read_commit_identity(repo, args.commit)
            validate_commit_identity(author, committer)
            checked = 1
    except IdentityCheckError as exc:
        print(f"Commit identity check: FAIL\n- {exc}")
        return 1
    print(
        "Commit identity check: PASS "
        f"(commits={checked}; author=noreply; committer=allowed-noreply)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
