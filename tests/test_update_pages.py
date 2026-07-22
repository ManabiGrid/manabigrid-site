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

    def test_approved_source_drift_is_rejected(self) -> None:
        approved = "a" * 40
        with patch.object(update_pages, "remote_sha", return_value="b" * 40):
            with self.assertRaises(update_pages.UpdateError) as caught:
                update_pages.choose_source(approved)
        self.assertEqual(caught.exception.status, "blocked_source_drift")

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
