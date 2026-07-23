from __future__ import annotations

import json
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
            patch.object(update_pages, "write_report") as write_report,
        ):
            payload = update_pages.verify_site_release(args)
        self.assertEqual(payload["status"], "site_release_verified")
        self.assertEqual(payload["site_sha"], self.SITE_SHA)
        self.assertEqual(payload["source_sha"], source_sha)
        find_run.assert_called_once_with(self.SITE_SHA, 1)
        wait_run.assert_called_once_with(run, 1)
        wait_live.assert_called_once_with(self.SITE_SHA, source_sha, 1)
        verify_deployment.assert_called_once_with(self.SITE_SHA)
        write_report.assert_called_once_with(payload)

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
            patch.object(update_pages, "write_report"),
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.publish(args)
        self.assertEqual(caught.exception.status, "blocked_source_drift_after_publish")
        self.assertEqual(dispatched, [approved])


if __name__ == "__main__":
    unittest.main()
