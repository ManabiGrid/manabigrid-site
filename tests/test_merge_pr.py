from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import merge_pr


def completed(
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def open_pr_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 17,
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "state": "OPEN",
        "isDraft": False,
        "url": "https://github.com/ManabiGrid/manabigrid-site/pull/17",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [
            {
                "name": merge_pr.REQUIRED_CHECK_NAME,
                "workflowName": merge_pr.REQUIRED_WORKFLOW_NAME,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
    }
    payload.update(overrides)
    return payload


class MergePrTests(unittest.TestCase):
    def test_merge_command_requires_noreply_and_locked_head(self) -> None:
        command = merge_pr.build_merge_command(
            "17",
            "12345+fixture-user@users.noreply.github.com",
            "a" * 40,
        )
        self.assertIn("--author-email", command)
        self.assertEqual(
            command[command.index("--author-email") + 1],
            "12345+fixture-user@users.noreply.github.com",
        )
        self.assertIn("--match-head-commit", command)
        self.assertEqual(
            command[command.index("--match-head-commit") + 1],
            "a" * 40,
        )
        for forbidden in (
            "--admin",
            "--auto",
            "--delete-branch",
            "--squash",
            "--rebase",
        ):
            self.assertNotIn(forbidden, command)

    def test_missing_approval_stops_before_any_command(self) -> None:
        output = io.StringIO()
        with (
            patch.object(merge_pr, "run_command") as run_command,
            redirect_stdout(output),
        ):
            result = merge_pr.main(["17"])
        self.assertEqual(result, 2)
        run_command.assert_not_called()
        self.assertIn("STOPPED", output.getvalue())

    def test_approved_merge_binds_review_gate_head_and_postflight(
        self,
    ) -> None:
        noreply = "12345+fixture-user@users.noreply.github.com"
        head = "a" * 40
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha=head,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        output = io.StringIO()
        with (
            patch.object(
                merge_pr,
                "load_authenticated_user",
                return_value=merge_pr.AuthenticatedUser(12345, "fixture-user"),
            ),
            patch.object(
                merge_pr,
                "load_local_author_email",
                return_value=noreply,
            ),
            patch.object(merge_pr, "verify_pages_environment_policy"),
            patch.object(
                merge_pr,
                "load_pull_request",
                return_value=pull_request,
            ),
            patch.object(
                merge_pr,
                "validate_pull_request_commits",
                return_value=1,
            ) as validate_commits,
            patch.object(
                merge_pr,
                "run_command",
                return_value=completed(),
            ) as run_command,
            patch.object(
                merge_pr,
                "verify_merged_result",
                return_value="b" * 40,
            ) as verify_result,
            patch.object(
                merge_pr,
                "verify_remote_merge_commit",
            ) as verify_commit,
            redirect_stdout(output),
        ):
            result = merge_pr.main(
                [
                    "17",
                    "--approve-merge",
                    "--reviewed-head-sha",
                    head,
                ]
            )
        self.assertEqual(result, 0, output.getvalue())
        merge_command = run_command.call_args.args[0]
        self.assertEqual(
            merge_command[merge_command.index("--author-email") + 1],
            noreply,
        )
        self.assertEqual(
            merge_command[merge_command.index("--match-head-commit") + 1],
            head,
        )
        validate_commits.assert_called_once_with(pull_request)
        verify_result.assert_called_once_with("17", head)
        verify_commit.assert_called_once_with("b" * 40, head)
        self.assertNotIn(noreply, output.getvalue())

    def test_reviewed_head_must_match_live_pr(self) -> None:
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha="a" * 40,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        with (
            patch.object(
                merge_pr,
                "load_authenticated_user",
                return_value=merge_pr.AuthenticatedUser(12345, "fixture-user"),
            ),
            patch.object(
                merge_pr,
                "load_local_author_email",
                return_value="12345+fixture-user@users.noreply.github.com",
            ),
            patch.object(merge_pr, "verify_pages_environment_policy"),
            patch.object(
                merge_pr,
                "load_pull_request",
                return_value=pull_request,
            ),
            patch.object(merge_pr, "run_command") as run_command,
            redirect_stdout(io.StringIO()),
        ):
            result = merge_pr.main(
                [
                    "17",
                    "--approve-merge",
                    "--reviewed-head-sha",
                    "b" * 40,
                ]
            )
        self.assertEqual(result, 1)
        run_command.assert_not_called()

    def test_local_email_must_match_authenticated_noreply(self) -> None:
        user = merge_pr.AuthenticatedUser(
            database_id=12345,
            login="fixture-user",
        )
        private = "private@example.test"
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(private + "\n"),
        ):
            with self.assertRaises(merge_pr.MergeGuardError) as raised:
                merge_pr.load_local_author_email(user)
        self.assertNotIn(private, str(raised.exception))

    def test_local_email_uses_repo_local_configuration(self) -> None:
        user = merge_pr.AuthenticatedUser(12345, "fixture-user")
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(
                "12345+fixture-user@users.noreply.github.com\n"
            ),
        ) as run_command:
            merge_pr.load_local_author_email(user)
        self.assertEqual(
            run_command.call_args.args[0],
            ["git", "config", "--local", "--get", "user.email"],
        )

    def test_pages_environment_must_allow_only_main(self) -> None:
        valid_environment = json.dumps(
            {
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                }
            }
        )
        valid_branches = json.dumps(
            {
                "total_count": 1,
                "branch_policies": [{"name": "main", "type": "branch"}],
            }
        )
        with patch.object(
            merge_pr,
            "run_command",
            side_effect=(
                completed(valid_environment),
                completed(valid_branches),
            ),
        ):
            merge_pr.verify_pages_environment_policy()
        invalid_branches = json.dumps(
            {
                "total_count": 1,
                "branch_policies": [{"name": "*", "type": "branch"}],
            }
        )
        with patch.object(
            merge_pr,
            "run_command",
            side_effect=(
                completed(valid_environment),
                completed(invalid_branches),
            ),
        ):
            with self.assertRaises(merge_pr.MergeGuardError):
                merge_pr.verify_pages_environment_policy()

    def test_pr_head_must_be_lowercase_full_sha(self) -> None:
        payload = open_pr_payload(headRefOid="not-a-sha")
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(json.dumps(payload)),
        ):
            with self.assertRaises(merge_pr.MergeGuardError):
                merge_pr.load_pull_request("17")

    def test_draft_pr_is_rejected(self) -> None:
        payload = open_pr_payload(isDraft=True)
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(json.dumps(payload)),
        ):
            with self.assertRaises(merge_pr.MergeGuardError):
                merge_pr.load_pull_request("17")

    def test_required_pr_gate_must_be_unique_and_successful(self) -> None:
        for checks in (
            [],
            [
                {
                    "name": merge_pr.REQUIRED_CHECK_NAME,
                    "workflowName": merge_pr.REQUIRED_WORKFLOW_NAME,
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ],
            open_pr_payload()["statusCheckRollup"] * 2,
        ):
            with self.subTest(checks=checks):
                payload = open_pr_payload(statusCheckRollup=checks)
                with patch.object(
                    merge_pr,
                    "run_command",
                    return_value=completed(json.dumps(payload)),
                ):
                    with self.assertRaises(merge_pr.MergeGuardError):
                        merge_pr.load_pull_request("17")

    def test_pr_commit_range_rejects_non_noreply_intermediate(self) -> None:
        private = "private-intermediate@example.test"
        commits = [
            {
                "sha": "b" * 40,
                "author": private,
                "committer": "12345+fixture-user@users.noreply.github.com",
            },
            {
                "sha": "a" * 40,
                "author": "12345+fixture-user@users.noreply.github.com",
                "committer": "12345+fixture-user@users.noreply.github.com",
            },
        ]
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha="a" * 40,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(json.dumps(commits)),
        ):
            with self.assertRaises(merge_pr.MergeGuardError) as raised:
                merge_pr.validate_pull_request_commits(pull_request)
        self.assertNotIn(private, str(raised.exception))

    def test_pr_commit_pages_are_fully_checked_in_order(self) -> None:
        first = "11111+first-fixture@users.noreply.github.com"
        second = "22222+second-fixture@users.noreply.github.com"
        third = "33333+third-fixture@users.noreply.github.com"
        pages = (
            json.dumps(
                [
                    {
                        "sha": "b" * 40,
                        "author": first,
                        "committer": first,
                    }
                ]
            )
            + "\n"
            + json.dumps(
                [
                    {
                        "sha": "c" * 40,
                        "author": second,
                        "committer": second,
                    },
                    {
                        "sha": "a" * 40,
                        "author": third,
                        "committer": third,
                    },
                ]
            )
        )
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha="a" * 40,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        decoded = merge_pr.decode_paginated_commit_documents(pages)
        self.assertEqual(
            [commit["sha"] for commit in decoded],
            ["b" * 40, "c" * 40, "a" * 40],
        )
        with (
            patch.object(
                merge_pr,
                "run_command",
                return_value=completed(pages),
            ) as run_command,
            patch.object(
                merge_pr.check_commit_identity,
                "validate_commit_identity",
            ) as validate_identity,
        ):
            self.assertEqual(
                merge_pr.validate_pull_request_commits(pull_request),
                3,
            )
        self.assertEqual(
            [call.args for call in validate_identity.call_args_list],
            [(first, first), (second, second), (third, third)],
        )
        command = run_command.call_args.args[0]
        self.assertIn("--paginate", command)
        self.assertIn("--jq", command)
        self.assertNotIn("--slurp", command)
        query = command[command.index("--jq") + 1]
        self.assertTrue(query.startswith("map({sha:"))
        self.assertNotIn("map(.[])", query)

    def test_second_commit_page_rejects_private_identity(self) -> None:
        noreply = "12345+fixture-user@users.noreply.github.com"
        private = "private-second-page@example.test"
        pages = (
            json.dumps(
                [
                    {
                        "sha": "b" * 40,
                        "author": noreply,
                        "committer": noreply,
                    }
                ]
            )
            + "\n"
            + json.dumps(
                [
                    {
                        "sha": "c" * 40,
                        "author": private,
                        "committer": noreply,
                    },
                    {
                        "sha": "a" * 40,
                        "author": noreply,
                        "committer": noreply,
                    },
                ]
            )
        )
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha="a" * 40,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(pages),
        ):
            with self.assertRaises(merge_pr.MergeGuardError) as raised:
                merge_pr.validate_pull_request_commits(pull_request)
        self.assertNotIn(private, str(raised.exception))

    def test_malformed_and_trailing_commit_pages_are_rejected(self) -> None:
        noreply = "12345+fixture-user@users.noreply.github.com"
        valid_page = json.dumps(
            [
                {
                    "sha": "a" * 40,
                    "author": noreply,
                    "committer": noreply,
                }
            ]
        )
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha="a" * 40,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        for raw in (
            valid_page[:-1],
            valid_page + "\ntrailing",
            valid_page + "\n{}",
        ):
            with self.subTest(raw_suffix=raw[-12:]):
                with patch.object(
                    merge_pr,
                    "run_command",
                    return_value=completed(raw),
                ):
                    with self.assertRaises(merge_pr.MergeGuardError):
                        merge_pr.validate_pull_request_commits(pull_request)

    def test_main_stops_before_merge_on_malformed_commit_pages(self) -> None:
        private = "private-trailing@example.test"
        head = "a" * 40
        noreply = "12345+fixture-user@users.noreply.github.com"
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha=head,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        malformed = (
            json.dumps(
                [
                    {
                        "sha": head,
                        "author": noreply,
                        "committer": noreply,
                    }
                ]
            )
            + "\n"
            + private
        )
        output = io.StringIO()
        with (
            patch.object(
                merge_pr,
                "load_authenticated_user",
                return_value=merge_pr.AuthenticatedUser(
                    12345,
                    "fixture-user",
                ),
            ),
            patch.object(merge_pr, "verify_pages_environment_policy"),
            patch.object(
                merge_pr,
                "load_local_author_email",
                return_value=noreply,
            ),
            patch.object(
                merge_pr,
                "load_pull_request",
                return_value=pull_request,
            ),
            patch.object(
                merge_pr,
                "run_command",
                return_value=completed(malformed),
            ) as run_command,
            redirect_stdout(output),
        ):
            result = merge_pr.main(
                [
                    "17",
                    "--approve-merge",
                    "--reviewed-head-sha",
                    head,
                ]
            )
        self.assertEqual(result, 1)
        run_command.assert_called_once()
        command = run_command.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "api", "--paginate"])
        self.assertNotIn("merge", command)
        self.assertNotIn(private, output.getvalue())
        self.assertIn("FAIL", output.getvalue())

    def test_pr_commit_list_must_end_at_reviewed_head(self) -> None:
        commits = [
            {
                "sha": "b" * 40,
                "author": "12345+fixture-user@users.noreply.github.com",
                "committer": "12345+fixture-user@users.noreply.github.com",
            }
        ]
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha="a" * 40,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(json.dumps(commits)),
        ):
            with self.assertRaises(merge_pr.MergeGuardError):
                merge_pr.validate_pull_request_commits(pull_request)

    def test_post_merge_pending_state_is_not_reported_as_success(self) -> None:
        payload = {
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "mergeCommit": None,
            "url": "https://github.com/ManabiGrid/manabigrid-site/pull/17",
        }
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(json.dumps(payload)),
        ):
            with self.assertRaises(merge_pr.MergeGuardError):
                merge_pr.verify_merged_result("17", "a" * 40)

    def test_ambiguous_command_is_success_only_after_server_verification(
        self,
    ) -> None:
        head = "a" * 40
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha=head,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        output = io.StringIO()
        with (
            patch.object(
                merge_pr,
                "load_authenticated_user",
                return_value=merge_pr.AuthenticatedUser(12345, "fixture-user"),
            ),
            patch.object(
                merge_pr,
                "load_local_author_email",
                return_value="12345+fixture-user@users.noreply.github.com",
            ),
            patch.object(merge_pr, "verify_pages_environment_policy"),
            patch.object(
                merge_pr,
                "load_pull_request",
                return_value=pull_request,
            ),
            patch.object(
                merge_pr,
                "validate_pull_request_commits",
                return_value=1,
            ),
            patch.object(
                merge_pr,
                "run_command",
                return_value=completed(returncode=1),
            ),
            patch.object(
                merge_pr,
                "verify_merged_result",
                return_value="b" * 40,
            ),
            patch.object(merge_pr, "verify_remote_merge_commit"),
            redirect_stdout(output),
        ):
            result = merge_pr.main(
                [
                    "17",
                    "--approve-merge",
                    "--reviewed-head-sha",
                    head,
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("ambiguous command result", output.getvalue())

    def test_failed_post_attempt_check_reports_unknown_and_no_retry(
        self,
    ) -> None:
        head = "a" * 40
        pull_request = merge_pr.PullRequest(
            number=17,
            head_sha=head,
            base_ref="main",
            state="OPEN",
            is_draft=False,
            url="https://github.com/ManabiGrid/manabigrid-site/pull/17",
        )
        output = io.StringIO()
        with (
            patch.object(
                merge_pr,
                "load_authenticated_user",
                return_value=merge_pr.AuthenticatedUser(12345, "fixture-user"),
            ),
            patch.object(
                merge_pr,
                "load_local_author_email",
                return_value="12345+fixture-user@users.noreply.github.com",
            ),
            patch.object(merge_pr, "verify_pages_environment_policy"),
            patch.object(
                merge_pr,
                "load_pull_request",
                return_value=pull_request,
            ),
            patch.object(
                merge_pr,
                "validate_pull_request_commits",
                return_value=1,
            ),
            patch.object(
                merge_pr,
                "run_command",
                return_value=completed(returncode=1),
            ),
            patch.object(
                merge_pr,
                "verify_merged_result",
                side_effect=merge_pr.MergeGuardError("not observed"),
            ),
            redirect_stdout(output),
        ):
            result = merge_pr.main(
                [
                    "17",
                    "--approve-merge",
                    "--reviewed-head-sha",
                    head,
                ]
            )
        self.assertEqual(result, 3)
        self.assertIn("STATE UNKNOWN AFTER ATTEMPT", output.getvalue())
        self.assertIn("do not retry", output.getvalue())

    def test_remote_merge_must_have_reviewed_parent_and_safe_identity(
        self,
    ) -> None:
        private = "private@example.test"
        payload = {
            "sha": "b" * 40,
            "author": private,
            "committer": "noreply@github.com",
            "parents": ["c" * 40, "a" * 40],
        }
        with patch.object(
            merge_pr,
            "run_command",
            return_value=completed(json.dumps(payload)),
        ):
            with self.assertRaises(merge_pr.MergeGuardError) as raised:
                merge_pr.verify_remote_merge_commit("b" * 40, "a" * 40)
        self.assertNotIn(private, str(raised.exception))

    def test_failure_output_does_not_relay_external_text(self) -> None:
        private = "private@example.test"
        with self.assertRaises(merge_pr.MergeGuardError) as raised:
            merge_pr.require_success(
                completed(
                    returncode=1,
                    stderr=f"remote rejected {private}",
                ),
                "fixture",
            )
        self.assertNotIn(private, str(raised.exception))
        self.assertIn("exit code 1", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
