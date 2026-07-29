from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from unittest.mock import patch

import update_pages


class UpdatePagesContractTests(unittest.TestCase):
    REQUEST_ID = "3f5cf4f0-1725-48f5-9fb8-3fa6e62962f7"
    SITE_SHA = "c" * 40

    def run_row(self, database_id: int = 2, title: str | None = None) -> dict[str, object]:
        return {
            "databaseId": database_id,
            "displayTitle": title or f"Pages / workflow_dispatch / {self.REQUEST_ID}",
            "event": "workflow_dispatch",
            "headBranch": "main",
            "headSha": self.SITE_SHA,
            "workflowName": update_pages.WORKFLOW_NAME,
            "url": "two",
        }

    def push_row(self, database_id: int = 3) -> dict[str, object]:
        return {
            "databaseId": database_id,
            "displayTitle": f"Pages / push / {self.SITE_SHA}",
            "event": "push",
            "headBranch": "main",
            "headSha": self.SITE_SHA,
            "workflowName": update_pages.WORKFLOW_NAME,
            "url": "push-run",
        }

    def test_full_sha_accepts_only_lowercase_hex(self) -> None:
        self.assertTrue(update_pages.is_full_sha("a" * 40))
        self.assertFalse(update_pages.is_full_sha("A" * 40))
        self.assertFalse(update_pages.is_full_sha("a" * 39))
        self.assertFalse(update_pages.is_full_sha("z" * 40))

    def test_github_cli_environment_cannot_redirect_to_another_host(self) -> None:
        with patch.dict(
            update_pages.os.environ,
            {
                "GH_HOST": "github.example.invalid",
                "GH_REPO": "example/wrong-repository",
            },
        ):
            environment = update_pages.command_environment(("gh", "run", "list"))
        self.assertEqual(environment["GH_HOST"], "github.com")
        self.assertNotIn("GH_REPO", environment)

    def test_git_read_environment_disables_optional_index_locks(self) -> None:
        with patch.dict(
            update_pages.os.environ,
            {"GIT_OPTIONAL_LOCKS": "1"},
        ):
            environment = update_pages.command_environment(
                ("git", "status", "--porcelain")
            )
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")

    def test_request_id_selects_only_its_run(self) -> None:
        request_id = self.REQUEST_ID
        runs = [
            {"databaseId": 1, "displayTitle": "Pages / push / abc", "url": "one"},
            self.run_row(),
        ]
        match = update_pages.match_request_run(runs, request_id, self.SITE_SHA)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.database_id, 2)

    def test_request_id_does_not_guess_latest_run(self) -> None:
        runs = [{"databaseId": 9, "displayTitle": "Pages / workflow_dispatch / other", "url": "nine"}]
        self.assertIsNone(update_pages.match_request_run(runs, self.REQUEST_ID, self.SITE_SHA))

    def test_request_id_partial_title_is_not_accepted(self) -> None:
        runs = [self.run_row(title=f"prefix-{self.REQUEST_ID}-suffix")]
        self.assertIsNone(update_pages.match_request_run(runs, self.REQUEST_ID, self.SITE_SHA))

    def test_request_id_run_metadata_must_match(self) -> None:
        run = self.run_row()
        run["headBranch"] = "other"
        with self.assertRaises(update_pages.UpdateError) as caught:
            update_pages.match_request_run([run], self.REQUEST_ID, self.SITE_SHA)
        self.assertEqual(caught.exception.status, "failed_run_correlation")

    def test_duplicate_request_id_is_rejected(self) -> None:
        runs = [
            self.run_row(database_id=1),
            self.run_row(database_id=2),
        ]
        with self.assertRaises(update_pages.UpdateError) as caught:
            update_pages.match_request_run(runs, self.REQUEST_ID, self.SITE_SHA)
        self.assertEqual(caught.exception.status, "failed_run_correlation")

    def test_push_run_matches_exact_site_commit_and_metadata(self) -> None:
        match = update_pages.match_push_run([self.push_row()], self.SITE_SHA)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.database_id, 3)

    def test_push_run_rejects_wrong_metadata(self) -> None:
        row = self.push_row()
        row["headBranch"] = "other"
        with self.assertRaises(update_pages.UpdateError) as caught:
            update_pages.match_push_run([row], self.SITE_SHA)
        self.assertEqual(caught.exception.status, "failed_run_correlation")

    def test_duplicate_push_run_is_rejected(self) -> None:
        with self.assertRaises(update_pages.UpdateError) as caught:
            update_pages.match_push_run(
                [self.push_row(3), self.push_row(4)], self.SITE_SHA
            )
        self.assertEqual(caught.exception.status, "failed_run_correlation")

    def test_verify_site_release_binds_push_live_and_pages_to_exact_shas(self) -> None:
        source_sha = "a" * 40
        args = Namespace(
            site_sha=self.SITE_SHA,
            source_sha=source_sha,
            correlation_timeout=1,
            run_timeout=1,
            live_timeout=1,
        )
        run = update_pages.RunMatch(3, f"Pages / push / {self.SITE_SHA}", "push-run")
        checkout = {"site_local": self.SITE_SHA, "site_remote": self.SITE_SHA}
        with (
            patch.object(update_pages, "require_release_checkout", return_value=checkout),
            patch.object(update_pages, "choose_source", return_value=source_sha),
            patch.object(update_pages, "find_push_run", return_value=run) as find_run,
            patch.object(update_pages, "wait_for_run") as wait_run,
            patch.object(update_pages, "wait_for_site_live") as wait_live,
            patch.object(update_pages, "verify_pages_deployment") as verify_deployment,
            patch.object(update_pages, "write_release_record") as write_record,
        ):
            payload = update_pages.verify_site_release(args)
        self.assertEqual(payload["status"], "site_release_verified")
        self.assertEqual(payload["record_type"], "publication_verification")
        self.assertIs(payload["publication_verified"], True)
        self.assertEqual(payload["site_sha"], self.SITE_SHA)
        self.assertEqual(payload["source_sha"], source_sha)
        find_run.assert_called_once_with(self.SITE_SHA, 1)
        wait_run.assert_called_once_with(run, 1)
        wait_live.assert_called_once_with(self.SITE_SHA, source_sha, 1)
        verify_deployment.assert_called_once_with(self.SITE_SHA)
        write_record.assert_called_once_with(payload)

    def release_payload(self, status: str = "updated") -> dict[str, object]:
        return update_pages.release_meta(
            status,
            {
                "runs": [
                    {
                        "database_id": 7,
                        "title": "verified run",
                        "url": "https://github.com/ManabiGrid/manabigrid-site/actions/runs/7",
                    }
                ],
                "site_local": self.SITE_SHA,
                "site_remote": self.SITE_SHA,
                "site_origin": (
                    "https://github.com/manabigrid/manabigrid-site.git"
                ),
                "source_sha": "a" * 40,
            },
        )

    def test_status_main_success_is_stdout_only(self) -> None:
        snapshot = update_pages.publish_meta(
            "current",
            {"source_sync": "current"},
            record_type=update_pages.STATUS_RECORD_TYPE,
        )
        with (
            patch.object(update_pages, "status_payload", return_value=snapshot),
            patch.object(update_pages, "write_release_record") as write_record,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = update_pages.main(["status"])
        self.assertEqual(result, 0)
        write_record.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), snapshot)

    def test_status_main_expected_error_is_stdout_only(self) -> None:
        with (
            patch.object(
                update_pages,
                "status_payload",
                side_effect=update_pages.UpdateError(
                    "failed_command",
                    "status failure",
                ),
            ),
            patch.object(update_pages, "write_release_record") as write_record,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = update_pages.main(["status"])
        self.assertEqual(result, 1)
        write_record.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["record_type"], "operation_error")
        self.assertEqual(payload["status"], "failed_command")

    def test_status_main_generic_error_is_stdout_only(self) -> None:
        with (
            patch.object(
                update_pages,
                "status_payload",
                side_effect=OSError("read failure"),
            ),
            patch.object(update_pages, "write_release_record") as write_record,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = update_pages.main(["status"])
        self.assertEqual(result, 1)
        write_record.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["record_type"], "operation_error")
        self.assertEqual(payload["status"], "failed_live_verify")

    def test_status_payload_uses_only_reviewed_read_only_operations(
        self,
    ) -> None:
        created_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        source_remote = update_pages.source_remote()
        workflow_api = (
            "repos/ManabiGrid/manabigrid-site/"
            "actions/workflows/pages.yml"
        )
        schedule_command = (
            "gh",
            "run",
            "list",
            "--repo",
            "ManabiGrid/manabigrid-site",
            "--workflow",
            "pages.yml",
            "--event",
            "schedule",
            "--limit",
            "1",
            "--json",
            (
                "createdAt,conclusion,status,url,headSha,"
                "headBranch"
            ),
        )
        expected_operations = [
            ("git", "rev-parse", "HEAD"),
            ("git", "remote", "get-url", "origin"),
            ("git", "ls-remote", "origin", "refs/heads/main"),
            (
                "git",
                "ls-remote",
                source_remote,
                "refs/heads/main",
            ),
            ("gh", "api", workflow_api),
            schedule_command,
            (
                "git",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ),
            ("git", "branch", "--show-current"),
            (update_pages.sys.executable, "check_workflow.py"),
            (update_pages.sys.executable, "check_pr_workflow.py"),
        ]
        command_map = {
            expected_operations[0]: self.SITE_SHA,
            expected_operations[1]: (
                "https://github.com/ManabiGrid/manabigrid-site.git"
            ),
            expected_operations[2]: f"{self.SITE_SHA}\trefs/heads/main",
            expected_operations[3]: f"{'a' * 40}\trefs/heads/main",
            expected_operations[4]: json.dumps({"state": "active"}),
            expected_operations[5]: json.dumps(
                [
                    {
                        "createdAt": created_at,
                        "conclusion": "success",
                        "status": "completed",
                        "url": (
                            "https://github.com/ManabiGrid/"
                            "manabigrid-site/actions/runs/1"
                        ),
                        "headSha": self.SITE_SHA,
                        "headBranch": "main",
                    }
                ]
            ),
            expected_operations[6]: "",
            expected_operations[7]: "main",
            expected_operations[8]: "PASS",
            expected_operations[9]: "PASS",
        }
        observed: list[tuple[str, ...]] = []

        def strict_read_only_command(
            arguments: object,
            **_kwargs: object,
        ) -> str:
            operation = tuple(arguments)
            if operation not in command_map:
                raise AssertionError(
                    f"status attempted an unreviewed operation: {operation}"
                )
            observed.append(operation)
            return command_map[operation]

        with tempfile.TemporaryDirectory() as directory:
            missing_record = Path(directory) / "update-report.json"
            with (
                patch.object(
                    update_pages,
                    "REPORT_PATH",
                    missing_record,
                ),
                patch.object(
                    update_pages,
                    "command",
                    side_effect=strict_read_only_command,
                ),
                patch.object(
                    update_pages,
                    "published_report",
                    return_value={
                        "source": {"commit": "a" * 40},
                        "publication": {"site_commit": self.SITE_SHA},
                    },
                ),
                patch.object(
                    update_pages,
                    "write_release_record",
                ) as write_record,
                patch.object(update_pages, "dispatch") as dispatch,
                patch.object(
                    update_pages.subprocess,
                    "run",
                    side_effect=AssertionError(
                        "status attempted direct subprocess execution"
                    ),
                ),
                patch.object(
                    update_pages,
                    "urlopen",
                    side_effect=AssertionError(
                        "status attempted an unreviewed network request"
                    ),
                ),
            ):
                payload = update_pages.status_payload()
        self.assertEqual(observed, expected_operations)
        self.assertEqual(payload["record_type"], "status_snapshot")
        write_record.assert_not_called()
        dispatch.assert_not_called()

    def test_release_record_rejects_status_snapshot_and_preserves_existing_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "update-report.json"
            report_path.write_text("previous verified record\n", encoding="utf-8")
            snapshot = update_pages.publish_meta(
                "current",
                {"source_sync": "current"},
                record_type=update_pages.STATUS_RECORD_TYPE,
            )
            with patch.object(update_pages, "REPORT_PATH", report_path):
                with self.assertRaises(update_pages.UpdateError) as caught:
                    update_pages.write_release_record(snapshot)
            self.assertEqual(caught.exception.status, "failed_release_record")
            self.assertEqual(
                report_path.read_text(encoding="utf-8"),
                "previous verified record\n",
            )

    def test_release_record_is_written_atomically_after_validation(self) -> None:
        payload = self.release_payload()
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "update-report.json"
            with patch.object(update_pages, "REPORT_PATH", report_path):
                update_pages.write_release_record(payload)
                stored = json.loads(report_path.read_text(encoding="utf-8"))
                leftovers = list(report_path.parent.glob(".update-report.json.*.tmp"))
        self.assertEqual(stored, payload)
        self.assertEqual(leftovers, [])

    def test_release_record_replace_failure_preserves_previous_record(self) -> None:
        payload = self.release_payload()
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "update-report.json"
            report_path.write_text("previous verified record\n", encoding="utf-8")
            with (
                patch.object(update_pages, "REPORT_PATH", report_path),
                patch.object(
                    update_pages.os,
                    "replace",
                    side_effect=OSError("replace denied"),
                ),
            ):
                with self.assertRaises(update_pages.UpdateError) as caught:
                    update_pages.write_release_record(payload)
            self.assertEqual(caught.exception.status, "failed_release_record")
            self.assertEqual(
                report_path.read_text(encoding="utf-8"),
                "previous verified record\n",
            )
            self.assertEqual(
                list(report_path.parent.glob(".update-report.json.*.tmp")),
                [],
            )

    def test_release_record_rejects_contradictory_site_identifiers(self) -> None:
        payload = self.release_payload()
        payload["site_sha"] = self.SITE_SHA
        payload["site_local"] = "b" * 40
        with self.assertRaises(update_pages.UpdateError) as caught:
            update_pages.validate_release_record(payload)
        self.assertEqual(caught.exception.status, "failed_release_record")

    def test_release_record_rejects_unverified_run_identity(self) -> None:
        for mutation in (
            {
                "database_id": 7,
                "title": "",
                "url": (
                    "https://github.com/ManabiGrid/"
                    "manabigrid-site/actions/runs/7"
                ),
            },
            {
                "database_id": 7,
                "title": "verified run",
                "url": "https://example.invalid/actions/runs/7",
            },
        ):
            with self.subTest(mutation=mutation):
                payload = self.release_payload()
                payload["runs"] = [mutation]
                with self.assertRaises(update_pages.UpdateError) as caught:
                    update_pages.validate_release_record(payload)
                self.assertEqual(
                    caught.exception.status,
                    "failed_release_record",
                )

    def test_release_record_summary_marks_legacy_snapshot_untrusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "update-report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schema_version": "4",
                        "status": "current",
                        "record_type": "status_snapshot",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(update_pages, "REPORT_PATH", report_path):
                summary = update_pages.release_record_summary()
        self.assertEqual(summary["state"], "legacy_or_invalid")

    def test_release_record_summary_exposes_only_verified_identifiers(self) -> None:
        payload = self.release_payload()
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "update-report.json"
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(update_pages, "REPORT_PATH", report_path):
                summary = update_pages.release_record_summary()
        self.assertEqual(
            summary,
            {
                "state": "valid",
                "status": "updated",
                "verified_at": payload["verified_at"],
                "site_sha": self.SITE_SHA,
                "source_sha": "a" * 40,
                "runs": 1,
            },
        )

    def test_approved_source_drift_is_rejected(self) -> None:
        approved = "a" * 40
        with patch.object(update_pages, "remote_sha", return_value="b" * 40):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.choose_source(approved)
        self.assertEqual(caught.exception.status, "blocked_source_drift")

    def test_site_origin_normalization_accepts_only_expected_github_repo(self) -> None:
        self.assertEqual(
            update_pages.normalize_github_remote(
                "git@github.com:ManabiGrid/manabigrid-site.git"
            ),
            "manabigrid/manabigrid-site",
        )
        self.assertEqual(
            update_pages.normalize_github_remote(
                "https://github.com/ManabiGrid/manabigrid-site.git"
            ),
            "manabigrid/manabigrid-site",
        )
        self.assertEqual(
            update_pages.normalize_github_remote(
                "ssh://git@github.com/ManabiGrid/manabigrid-site.git"
            ),
            "manabigrid/manabigrid-site",
        )
        self.assertIsNone(
            update_pages.normalize_github_remote(
                "https://token@github.com/ManabiGrid/manabigrid-site.git"
            )
        )
        self.assertNotEqual(
            update_pages.normalize_github_remote(
                "https://github.com/example/manabigrid-site.git"
            ),
            update_pages.expected_site_repository(),
        )

    def test_release_trust_anchors_reject_mutable_config_drift(self) -> None:
        with patch.dict(
            update_pages.CONFIG,
            {"site_repository": "example/manabigrid-site"},
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.official_site_repository()
        self.assertEqual(caught.exception.status, "blocked_config_drift")
        with patch.dict(
            update_pages.CONFIG,
            {"base_url": "https://example.invalid/"},
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.official_base_url()
        self.assertEqual(caught.exception.status, "blocked_config_drift")
        with patch.dict(
            update_pages.CONFIG,
            {"source_repository_url": "https://github.com/example/fork"},
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.official_source_repository_url()
        self.assertEqual(caught.exception.status, "blocked_config_drift")

    def test_release_checkout_rejects_fork_origin_before_remote_lookup(self) -> None:
        with (
            patch.object(
                update_pages,
                "site_origin",
                return_value="https://github.com/example/manabigrid-site.git",
            ),
            patch.object(update_pages, "remote_sha") as remote,
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.require_release_checkout()
        self.assertEqual(caught.exception.status, "blocked_site_origin")
        remote.assert_not_called()

    def test_status_shows_update_blocked_when_site_dirty(self) -> None:
        local = self.SITE_SHA
        remote = self.SITE_SHA
        source = "a" * 40
        command_map = {
            ("git", "rev-parse", "HEAD"): local,
            ("git", "remote", "get-url", "origin"): "https://github.com/ManabiGrid/manabigrid-site.git",
            ("git", "ls-remote", "origin", "refs/heads/main"): remote,
            ("git", "status", "--porcelain", "--untracked-files=all"): " M dirty",
            ("git", "branch", "--show-current"): "main",
        }
        with patch.object(
            update_pages,
            "command",
            side_effect=lambda args, **_: command_map.get(tuple(args), ""),
        ):
            with patch.object(
                update_pages,
                "remote_sha",
                side_effect=lambda remote_url: source if remote_url == update_pages.source_remote() else remote,
            ):
                with (
                    patch.object(
                        update_pages,
                        "published_report",
                        return_value={
                            "source": {"commit": source},
                            "publication": {"site_commit": local},
                        },
                    ),
                    patch.object(
                        update_pages,
                        "workflow_operational_status",
                        return_value={"status": "active"},
                    ),
                ):
                    payload = update_pages.status_payload()
        self.assertEqual(payload["source_sync"], "current")
        self.assertEqual(payload["release_readiness"], "blocked_dirty_site")
        self.assertNotEqual(payload["status"], "current")
        self.assertEqual(payload["status"], "blocked_dirty_site")
        self.assertEqual(
            payload["next_action_code"], "preserve_and_inspect_dirty_worktree"
        )

    def test_status_never_calls_dirty_checkout_ready_when_source_has_update(self) -> None:
        local = self.SITE_SHA
        source = "a" * 40
        published = "b" * 40
        command_map = {
            ("git", "rev-parse", "HEAD"): local,
            ("git", "remote", "get-url", "origin"): "git@github.com:ManabiGrid/manabigrid-site.git",
            ("git", "status", "--porcelain", "--untracked-files=all"): " M dirty",
            ("git", "branch", "--show-current"): "main",
            (update_pages.sys.executable, "check_workflow.py"): "PASS",
        }
        with patch.object(
            update_pages,
            "command",
            side_effect=lambda args, **_: command_map.get(tuple(args), ""),
        ):
            with patch.object(
                update_pages,
                "remote_sha",
                side_effect=lambda remote_url: source if remote_url == update_pages.source_remote() else local,
            ):
                with (
                    patch.object(
                        update_pages,
                        "published_report",
                        return_value={
                            "source": {"commit": published},
                            "publication": {"site_commit": local},
                        },
                    ),
                    patch.object(
                        update_pages,
                        "workflow_operational_status",
                        return_value={"status": "active"},
                    ),
                ):
                    payload = update_pages.status_payload()
        self.assertEqual(payload["source_sync"], "update_available")
        self.assertEqual(payload["release_readiness"], "blocked_dirty_site")
        self.assertEqual(payload["status"], "blocked_dirty_site")
        self.assertEqual(
            payload["next_action_code"], "preserve_and_inspect_dirty_worktree"
        )

    def test_status_does_not_infer_sync_when_published_report_is_unavailable(self) -> None:
        local = self.SITE_SHA
        source = "a" * 40
        command_map = {
            ("git", "rev-parse", "HEAD"): local,
            (
                "git",
                "remote",
                "get-url",
                "origin",
            ): "https://github.com/ManabiGrid/manabigrid-site.git",
            ("git", "status", "--porcelain", "--untracked-files=all"): "",
            ("git", "branch", "--show-current"): "main",
            (update_pages.sys.executable, "check_workflow.py"): "PASS",
        }
        with patch.object(
            update_pages,
            "command",
            side_effect=lambda args, **_: command_map.get(tuple(args), ""),
        ):
            with patch.object(
                update_pages,
                "remote_sha",
                side_effect=lambda remote_url: (
                    source
                    if remote_url == update_pages.source_remote()
                    else local
                ),
            ):
                with (
                    patch.object(update_pages, "published_report", return_value=None),
                    patch.object(
                        update_pages,
                        "workflow_operational_status",
                        return_value={"status": "active"},
                    ),
                ):
                    payload = update_pages.status_payload()
        self.assertEqual(payload["published_state"], "unknown")
        self.assertEqual(payload["source_sync"], "unknown")
        self.assertEqual(payload["site_sync"], "unknown")
        self.assertEqual(
            payload["release_readiness"],
            "blocked_published_state_unknown",
        )
        self.assertEqual(payload["status"], "blocked_published_state_unknown")
        self.assertEqual(payload["next_action_code"], "inspect_published_state")

    def test_status_preserves_configuration_drift_as_primary_block(self) -> None:
        local = self.SITE_SHA
        source = "a" * 40
        command_map = {
            ("git", "rev-parse", "HEAD"): local,
            (
                "git",
                "remote",
                "get-url",
                "origin",
            ): "https://github.com/ManabiGrid/manabigrid-site.git",
            ("git", "status", "--porcelain", "--untracked-files=all"): "",
            ("git", "branch", "--show-current"): "main",
            (update_pages.sys.executable, "check_workflow.py"): "PASS",
        }
        with patch.dict(
            update_pages.CONFIG,
            {"site_repository": "example/fork"},
        ):
            with patch.object(
                update_pages,
                "command",
                side_effect=lambda args, **_: command_map.get(tuple(args), ""),
            ):
                with patch.object(
                    update_pages,
                    "remote_sha",
                    side_effect=lambda remote_url: (
                        source
                        if remote_url
                        == update_pages.OFFICIAL_SOURCE_REPOSITORY_URL + ".git"
                        else local
                    ),
                ):
                    with (
                        patch.object(update_pages, "published_report") as report,
                        patch.object(
                            update_pages,
                            "workflow_operational_status",
                        ) as operational,
                    ):
                        payload = update_pages.status_payload()
        report.assert_not_called()
        operational.assert_not_called()
        self.assertEqual(payload["status"], "blocked_config_drift")
        self.assertEqual(payload["release_readiness"], "blocked_config_drift")
        self.assertEqual(payload["next_action_code"], "inspect_config_drift")
        self.assertFalse(payload["configuration"]["valid"])

    def test_status_blocks_when_pr_workflow_contract_fails(self) -> None:
        source = "a" * 40
        command_map = {
            ("git", "rev-parse", "HEAD"): self.SITE_SHA,
            (
                "git",
                "remote",
                "get-url",
                "origin",
            ): "https://github.com/ManabiGrid/manabigrid-site.git",
            ("git", "status", "--porcelain", "--untracked-files=all"): "",
            ("git", "branch", "--show-current"): "main",
            (update_pages.sys.executable, "check_workflow.py"): "PASS",
        }

        def checked_command(arguments: object, **_kwargs: object) -> str:
            key = tuple(arguments)
            if key == (update_pages.sys.executable, "check_pr_workflow.py"):
                raise update_pages.UpdateError(
                    "failed_command",
                    "PR workflow drift",
                )
            return command_map.get(key, "")

        with (
            patch.object(update_pages, "command", side_effect=checked_command),
            patch.object(
                update_pages,
                "remote_sha",
                side_effect=lambda remote_url: (
                    source
                    if remote_url == update_pages.source_remote()
                    else self.SITE_SHA
                ),
            ),
            patch.object(
                update_pages,
                "published_report",
                return_value={
                    "source": {"commit": source},
                    "publication": {"site_commit": self.SITE_SHA},
                },
            ),
            patch.object(
                update_pages,
                "workflow_operational_status",
                return_value={"status": "active"},
            ),
        ):
            payload = update_pages.status_payload()
        self.assertEqual(payload["status"], "blocked_contract_drift")
        self.assertFalse(payload["site"]["workflow_contract_pass"])
        self.assertEqual(
            payload["site"]["workflow_contracts"],
            {
                "check_workflow.py": True,
                "check_pr_workflow.py": False,
            },
        )

    def test_publish_flag_cannot_be_omitted(self) -> None:
        with self.assertRaises(update_pages.UpdateError) as caught:
            update_pages.publish(Namespace(approve_publication=False))
        self.assertEqual(caught.exception.status, "blocked_missing_approval")

    def test_publish_rejects_source_current_but_site_release_pending(self) -> None:
        source = "a" * 40
        args = Namespace(
            approve_publication=True,
            source_sha=source,
            dry_run=False,
            check_external_links=False,
            correlation_timeout=1,
            run_timeout=1,
            live_timeout=1,
        )
        with (
            patch.object(
                update_pages,
                "require_release_checkout",
                return_value={
                    "site_local": self.SITE_SHA,
                    "site_remote": self.SITE_SHA,
                    "site_origin": "official",
                },
            ),
            patch.object(update_pages, "choose_source", return_value=source),
            patch.object(
                update_pages,
                "published_report",
                return_value={
                    "source": {"commit": source},
                    "publication": {"site_commit": "d" * 40},
                },
            ),
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.publish(args)
        self.assertEqual(
            caught.exception.status,
            "blocked_site_release_requires_verification",
        )

    def test_workflow_operational_status_detects_active_schedule(self) -> None:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        responses = iter(
            [
                '{"state":"active"}',
                (
                    '[{"createdAt":'
                    + repr(created_at).replace("'", '"')
                    + ',"conclusion":"success","status":"completed","url":"run",'
                    + '"headSha":"'
                    + self.SITE_SHA
                    + '","headBranch":"main"}]'
                ),
            ]
        )
        with patch.object(update_pages, "command", side_effect=lambda *_args, **_kwargs: next(responses)):
            status = update_pages.workflow_operational_status(self.SITE_SHA)
        self.assertEqual(status["status"], "active")

    def test_schedule_run_must_exercise_current_site_revision(self) -> None:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        responses = iter(
            [
                '{"state":"active"}',
                (
                    '[{"createdAt":'
                    + repr(created_at).replace("'", '"')
                    + ',"conclusion":"success","status":"completed",'
                    + '"url":"run","headSha":"'
                    + ("d" * 40)
                    + '","headBranch":"main"}]'
                ),
            ]
        )
        with patch.object(
            update_pages,
            "command",
            side_effect=lambda *_args, **_kwargs: next(responses),
        ):
            status = update_pages.workflow_operational_status(self.SITE_SHA)
        self.assertEqual(status["status"], "unverified_revision")
        latest = status["latest_schedule"]
        assert isinstance(latest, dict)
        self.assertFalse(latest["head_matches_current"])

    def test_schedule_is_active_only_after_successful_completion(self) -> None:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for run_status, conclusion, expected in (
            ("completed", "skipped", "failed"),
            ("completed", "neutral", "failed"),
            ("in_progress", "", "in_progress"),
        ):
            with self.subTest(run_status=run_status, conclusion=conclusion):
                responses = iter(
                    [
                        '{"state":"active"}',
                        json.dumps(
                            [
                                {
                                    "createdAt": created_at,
                                    "conclusion": conclusion,
                                    "status": run_status,
                                    "url": "run",
                                    "headSha": self.SITE_SHA,
                                    "headBranch": "main",
                                }
                            ]
                        ),
                    ]
                )
                with patch.object(
                    update_pages,
                    "command",
                    side_effect=lambda *_args, **_kwargs: next(responses),
                ):
                    status = update_pages.workflow_operational_status(
                        self.SITE_SHA
                    )
                self.assertEqual(status["status"], expected)

    def test_schedule_run_from_another_branch_is_not_active(self) -> None:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        responses = iter(
            [
                '{"state":"active"}',
                json.dumps(
                    [
                        {
                            "createdAt": created_at,
                            "conclusion": "success",
                            "status": "completed",
                            "url": "run",
                            "headSha": self.SITE_SHA,
                            "headBranch": "preview",
                        }
                    ]
                ),
            ]
        )
        with patch.object(
            update_pages,
            "command",
            side_effect=lambda *_args, **_kwargs: next(responses),
        ):
            status = update_pages.workflow_operational_status(self.SITE_SHA)
        self.assertEqual(status["status"], "unverified_revision")
        latest = status["latest_schedule"]
        assert isinstance(latest, dict)
        self.assertFalse(latest["branch_matches_main"])

    def test_explicit_source_never_auto_follows_new_sha(self) -> None:
        approved = "a" * 40
        newer = "b" * 40
        args = Namespace(
            approve_publication=True,
            source_sha=approved,
            dry_run=False,
            check_external_links=False,
            correlation_timeout=1,
            run_timeout=1,
            live_timeout=1,
        )
        dispatched: list[str] = []
        run = update_pages.RunMatch(1, "title", "url")
        with (
            patch.object(update_pages, "require_release_checkout", return_value={"site_local": self.SITE_SHA, "site_remote": self.SITE_SHA}),
            patch.object(update_pages, "choose_source", return_value=approved),
            patch.object(update_pages, "published_report", return_value=None),
            patch.object(update_pages, "dispatch", side_effect=lambda sha, *_: dispatched.append(sha) or run),
            patch.object(update_pages, "wait_for_run"),
            patch.object(update_pages, "wait_for_site_live"),
            patch.object(update_pages, "verify_pages_deployment"),
            patch.object(update_pages, "remote_sha", return_value=newer),
            patch.object(update_pages, "write_release_record") as write_record,
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.publish(args)
        self.assertEqual(caught.exception.status, "blocked_source_drift_after_publish")
        self.assertIsNotNone(caught.exception.payload)
        self.assertEqual(dispatched, [approved])
        write_record.assert_called_once()
        record = write_record.call_args.args[0]
        self.assertEqual(record["record_type"], "publication_verification")
        self.assertEqual(record["status"], "blocked_source_drift_after_publish")
        self.assertIs(record["publication_verified"], True)

    def test_source_lookup_failure_after_publication_preserves_verified_record(
        self,
    ) -> None:
        approved = "a" * 40
        args = Namespace(
            approve_publication=True,
            source_sha=approved,
            dry_run=False,
            check_external_links=False,
            correlation_timeout=1,
            run_timeout=1,
            live_timeout=1,
        )
        run = update_pages.RunMatch(
            7,
            "Pages / workflow_dispatch / request",
            (
                "https://github.com/ManabiGrid/"
                "manabigrid-site/actions/runs/7"
            ),
        )
        checkout = {
            "site_local": self.SITE_SHA,
            "site_remote": self.SITE_SHA,
            "site_origin": (
                "https://github.com/manabigrid/manabigrid-site.git"
            ),
        }
        with (
            patch.object(
                update_pages,
                "require_release_checkout",
                return_value=checkout,
            ),
            patch.object(update_pages, "choose_source", return_value=approved),
            patch.object(update_pages, "published_report", return_value=None),
            patch.object(update_pages, "dispatch", return_value=run),
            patch.object(update_pages, "wait_for_run"),
            patch.object(update_pages, "wait_for_site_live") as wait_live,
            patch.object(
                update_pages,
                "verify_pages_deployment",
            ) as verify_deployment,
            patch.object(
                update_pages,
                "remote_sha",
                side_effect=update_pages.UpdateError(
                    "failed_command",
                    "network unavailable",
                ),
            ),
            patch.object(update_pages, "write_release_record") as write_record,
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.publish(args)
        self.assertEqual(
            caught.exception.status,
            "blocked_source_state_unknown_after_publish",
        )
        self.assertIsNotNone(caught.exception.payload)
        wait_live.assert_called_once_with(self.SITE_SHA, approved, 1)
        verify_deployment.assert_called_once_with(self.SITE_SHA)
        write_record.assert_called_once()
        record = write_record.call_args.args[0]
        self.assertEqual(
            record["status"],
            "blocked_source_state_unknown_after_publish",
        )
        self.assertIs(record["publication_verified"], True)
        self.assertEqual(record["source_freshness"], "unknown")
        self.assertEqual(record["freshness_error_status"], "failed_command")

    def test_failure_before_live_verification_does_not_touch_release_record(
        self,
    ) -> None:
        source = "a" * 40
        args = Namespace(
            approve_publication=True,
            source_sha=source,
            dry_run=False,
            check_external_links=False,
            correlation_timeout=1,
            run_timeout=1,
            live_timeout=1,
        )
        run = update_pages.RunMatch(1, "title", "url")
        with (
            patch.object(
                update_pages,
                "require_release_checkout",
                return_value={
                    "site_local": self.SITE_SHA,
                    "site_remote": self.SITE_SHA,
                },
            ),
            patch.object(update_pages, "choose_source", return_value=source),
            patch.object(update_pages, "published_report", return_value=None),
            patch.object(update_pages, "dispatch", return_value=run),
            patch.object(
                update_pages,
                "wait_for_run",
                side_effect=update_pages.UpdateError(
                    "failed_workflow",
                    "build failed",
                ),
            ),
            patch.object(update_pages, "write_release_record") as write_record,
        ):
            with self.assertRaises(update_pages.UpdateError):
                update_pages.publish(args)
        write_record.assert_not_called()

    def test_main_preserves_detailed_verified_drift_payload_without_second_write(
        self,
    ) -> None:
        payload = self.release_payload("blocked_source_drift_after_publish")
        payload["latest_source_sha"] = "b" * 40
        with (
            patch.object(
                update_pages,
                "publish",
                side_effect=update_pages.UpdateError(
                    "blocked_source_drift_after_publish",
                    "source moved",
                    payload=payload,
                ),
            ),
            patch.object(update_pages, "write_release_record") as write_record,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = update_pages.main(
                ["publish", "--approve-publication", "--source-sha", "a" * 40]
            )
        self.assertEqual(result, 1)
        write_record.assert_not_called()
        printed = json.loads(output.getvalue())
        self.assertEqual(printed["record_type"], "publication_verification")
        self.assertEqual(printed["latest_source_sha"], "b" * 40)
        self.assertEqual(printed["error"], "source moved")

    def test_compatibility_runbook_pins_fresh_roots_and_source_sha(
        self,
    ) -> None:
        contract = (
            Path(__file__).resolve().parents[1] / "UPDATE_CONTRACT.md"
        ).read_text(encoding="utf-8")
        section = contract.split("## 互換性修正が必要な場合", 1)[1].split(
            "\n## ",
            1,
        )[0]
        required_commands = (
            "set -euo pipefail",
            'SOURCE_ROOT="/absolute/path/to/clean-canonical-checkout"',
            'SOURCE_SHA="$(git ls-remote '
            "https://github.com/ManabiGrid/manabigrid.git "
            "refs/heads/main | awk '{print $1}')\"",
            'if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then',
            "canonical source main did not resolve to one "
            "lowercase 40-character SHA",
            'FRESH_OUTPUT="$(mktemp -d "$PWD/review/site-output.XXXXXX")"',
            'CHECK_REPORT="${FRESH_OUTPUT}.check-report.json"',
            'python3 build_site.py --source "$SOURCE_ROOT" '
            '--output "$FRESH_OUTPUT" --no-check '
            '--expected-source-sha "$SOURCE_SHA"',
            'python3 check_site.py "$FRESH_OUTPUT" '
            '--source "$SOURCE_ROOT" '
            '--expected-source-sha "$SOURCE_SHA" '
            '--report-output "$CHECK_REPORT"',
            'python3 package_site.py --site-root "$FRESH_OUTPUT" --dry-run',
            'python3 device_matrix_check.py --site-root "$FRESH_OUTPUT"',
            "python3 negative_css_overflow_check.py "
            '--site-root "$FRESH_OUTPUT"',
        )
        for command in required_commands:
            with self.subTest(command=command):
                self.assertIn(command, section)
        self.assertIn("site SHAを捏造しない", section)
        self.assertLess(
            section.index("set -euo pipefail"),
            section.index('SOURCE_SHA="$(git ls-remote'),
        )
        self.assertNotRegex(
            section,
            r"(?m)^python3 (?:build_site|check_site|package_site|"
            r"device_matrix_check|negative_css_overflow_check)\.py"
            r"(?: --no-check| --dry-run)?$",
        )

    def test_merge_runbook_uses_only_the_guarded_entrypoint(self) -> None:
        contract = (
            Path(__file__).resolve().parents[1] / "UPDATE_CONTRACT.md"
        ).read_text(encoding="utf-8")
        section = contract.split("## PR mergeのメール再発防止", 1)[1].split(
            "\n## ",
            1,
        )[0]
        self.assertIn(
            "python3 merge_pr.py <PR番号またはURL> --approve-merge "
            "--reviewed-head-sha <独立レビュー済みの40桁head SHA>",
            section,
        )
        self.assertIn(
            'BASE_SHA="$(git merge-base origin/main HEAD)"',
            section,
        )
        self.assertIn(
            'python3 check_commit_identity.py --since "$BASE_SHA" '
            '--commit "$HEAD_SHA"',
            section,
        )
        self.assertIn("--match-head-commit", section)
        self.assertIn("--author-email", section)
        self.assertIn(
            "実メール値や外部commandの本文は出力しない",
            section,
        )
        self.assertIn("固定privacy baseline", section)
        self.assertIn(
            "生の`gh pr merge`、Web UIのmergeボタン、`--admin`、"
            "`--auto`、`--delete-branch`、squash、rebaseへ迂回しない。",
            section,
        )
        self.assertIn("履歴改変・force push", section)
        bash_blocks = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
        self.assertEqual(len(bash_blocks), 2)
        self.assertEqual(
            bash_blocks[1].strip(),
            "python3 merge_pr.py <PR番号またはURL> --approve-merge "
            "--reviewed-head-sha <独立レビュー済みの40桁head SHA>",
        )
        for bash_block in bash_blocks:
            self.assertNotIn("gh pr merge", bash_block)

    def test_compatibility_runbook_stops_on_failure_and_invalid_sha(
        self,
    ) -> None:
        contract = (
            Path(__file__).resolve().parents[1] / "UPDATE_CONTRACT.md"
        ).read_text(encoding="utf-8")
        section = contract.split("## 互換性修正が必要な場合", 1)[1].split(
            "\n## ",
            1,
        )[0]
        bash_block = section.split("```bash", 1)[1].split("```", 1)[0]
        prologue = bash_block.split("SOURCE_ROOT=", 1)[0]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = dict(os.environ)

            command_marker = root / "continued-after-command-failure"
            environment["MARKER"] = str(command_marker)
            command_result = subprocess.run(
                [
                    "bash",
                    "-c",
                    prologue + 'false\nprintf reached > "$MARKER"\n',
                ],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertNotEqual(command_result.returncode, 0)
            self.assertFalse(command_marker.exists())

            pipeline_marker = root / "continued-after-pipeline-failure"
            environment["MARKER"] = str(pipeline_marker)
            pipeline_result = subprocess.run(
                [
                    "bash",
                    "-c",
                    prologue
                    + 'SOURCE_SHA="$(false | awk \'{print $1}\')"\n'
                    + 'printf reached > "$MARKER"\n',
                ],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertNotEqual(pipeline_result.returncode, 0)
            self.assertFalse(pipeline_marker.exists())

            sha_header = bash_block.split("mkdir -p review", 1)[0]
            sha_header = "\n".join(
                (
                    'SOURCE_SHA="not-a-sha"'
                    if line.startswith("SOURCE_SHA=")
                    else line
                )
                for line in sha_header.splitlines()
                if not line.startswith("SOURCE_ROOT=")
            )
            sha_marker = root / "continued-after-invalid-sha"
            environment["MARKER"] = str(sha_marker)
            sha_result = subprocess.run(
                [
                    "bash",
                    "-c",
                    sha_header + '\nprintf reached > "$MARKER"\n',
                ],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertNotEqual(sha_result.returncode, 0)
            self.assertFalse(sha_marker.exists())
            self.assertIn(
                "canonical source main did not resolve",
                sha_result.stderr,
            )


if __name__ == "__main__":
    unittest.main()
