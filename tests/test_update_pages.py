from __future__ import annotations

import unittest
from argparse import Namespace
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

    def test_status_shows_update_blocked_when_site_dirty(self) -> None:
        local = self.SITE_SHA
        remote = self.SITE_SHA
        source = "a" * 40
        command_map = {
            ("git", "rev-parse", "HEAD"): local,
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
                with patch.object(update_pages, "published_sha", return_value=source):
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
                with patch.object(update_pages, "published_sha", return_value=published):
                    payload = update_pages.status_payload()
        self.assertEqual(payload["source_sync"], "update_available")
        self.assertEqual(payload["release_readiness"], "blocked_dirty_site")
        self.assertEqual(payload["status"], "blocked_dirty_site")
        self.assertEqual(
            payload["next_action_code"], "preserve_and_inspect_dirty_worktree"
        )

    def test_publish_flag_cannot_be_omitted(self) -> None:
        with self.assertRaises(update_pages.UpdateError) as caught:
            update_pages.publish(Namespace(approve_publication=False))
        self.assertEqual(caught.exception.status, "blocked_missing_approval")

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
            patch.object(update_pages, "published_sha", return_value=None),
            patch.object(update_pages, "dispatch", side_effect=lambda sha, *_: dispatched.append(sha) or run),
            patch.object(update_pages, "wait_for_run"),
            patch.object(update_pages, "wait_for_live"),
            patch.object(update_pages, "remote_sha", return_value=newer),
            patch.object(update_pages, "write_report"),
        ):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.publish(args)
        self.assertEqual(caught.exception.status, "blocked_source_drift_after_publish")
        self.assertEqual(dispatched, [approved])


if __name__ == "__main__":
    unittest.main()
