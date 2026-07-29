from __future__ import annotations

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

import check_release_provenance


SITE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def merged_pr_payload() -> list[dict[str, object]]:
    return [
        {
            "number": 17,
            "state": "closed",
            "merged_at": "2026-07-29T00:00:00Z",
            "merge_commit_sha": SITE_SHA,
            "base": {"ref": "main"},
            "head": {"sha": HEAD_SHA},
        }
    ]


def successful_checks() -> dict[str, object]:
    return {
        "total_count": 1,
        "check_runs": [
            {
                "name": check_release_provenance.REQUIRED_CHECK_NAME,
                "head_sha": HEAD_SHA,
                "status": "completed",
                "conclusion": "success",
                "app": {"slug": "github-actions"},
            }
        ],
    }


def two_parent_merge() -> dict[str, object]:
    return {
        "sha": SITE_SHA,
        "parents": [
            {"sha": "c" * 40},
            {"sha": HEAD_SHA},
        ],
    }


class ReleaseProvenanceTests(unittest.TestCase):
    def test_exact_merged_pr_and_gate_pass(self) -> None:
        self.assertEqual(
            check_release_provenance.select_merged_pull_request(
                merged_pr_payload(),
                SITE_SHA,
            ),
            (17, HEAD_SHA),
        )
        check_release_provenance.validate_required_check(
            successful_checks(),
            HEAD_SHA,
        )
        check_release_provenance.validate_merge_parents(
            two_parent_merge(),
            SITE_SHA,
            HEAD_SHA,
        )

    def test_non_main_or_direct_push_commit_is_rejected(self) -> None:
        for payload in (
            [],
            [
                {
                    **merged_pr_payload()[0],
                    "base": {"ref": "feature"},
                }
            ],
            [
                merged_pr_payload()[0],
                {
                    **merged_pr_payload()[0],
                    "number": 18,
                },
            ],
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(
                    check_release_provenance.ProvenanceError
                ):
                    check_release_provenance.select_merged_pull_request(
                        payload,
                        SITE_SHA,
                    )

    def test_required_gate_must_be_unique_complete_and_successful(self) -> None:
        bad_payloads = (
            {"total_count": 0, "check_runs": []},
            {
                "total_count": 1,
                "check_runs": [
                    {
                        **successful_checks()["check_runs"][0],
                        "conclusion": "failure",
                    }
                ],
            },
            {
                "total_count": 2,
                "check_runs": successful_checks()["check_runs"],
            },
            {
                "total_count": 2,
                "check_runs": successful_checks()["check_runs"] * 2,
            },
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(
                    check_release_provenance.ProvenanceError
                ):
                    check_release_provenance.validate_required_check(
                        payload,
                        HEAD_SHA,
                    )

    def test_squash_rebase_wrong_head_and_octopus_are_rejected(self) -> None:
        bad_payloads = (
            {
                "sha": SITE_SHA,
                "parents": [{"sha": "c" * 40}],
            },
            {
                "sha": SITE_SHA,
                "parents": [
                    {"sha": "c" * 40},
                    {"sha": "d" * 40},
                ],
            },
            {
                "sha": SITE_SHA,
                "parents": [
                    {"sha": "c" * 40},
                    {"sha": HEAD_SHA},
                    {"sha": "d" * 40},
                ],
            },
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(
                    check_release_provenance.ProvenanceError
                ):
                    check_release_provenance.validate_merge_parents(
                        payload,
                        SITE_SHA,
                        HEAD_SHA,
                    )

    def test_non_main_workflow_dispatch_stops_before_network(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                check_release_provenance,
                "read_remote_main_sha",
            ) as remote,
            redirect_stdout(output),
        ):
            result = check_release_provenance.main(
                [
                    "--site-sha",
                    SITE_SHA,
                    "--ref",
                    "refs/heads/feature",
                ]
            )
        self.assertNotEqual(result, 0)
        remote.assert_not_called()
        self.assertIn("only from refs/heads/main", output.getvalue())

    def test_remote_main_mismatch_stops_before_api(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                check_release_provenance,
                "read_remote_main_sha",
                return_value="c" * 40,
            ),
            patch.object(
                check_release_provenance,
                "github_api",
            ) as github_api,
            redirect_stdout(output),
        ):
            result = check_release_provenance.main(
                [
                    "--site-sha",
                    SITE_SHA,
                    "--ref",
                    check_release_provenance.MAIN_REF,
                ]
            )
        self.assertNotEqual(result, 0)
        github_api.assert_not_called()
        self.assertIn("official remote main", output.getvalue())

    def test_external_api_error_body_is_not_relayed(self) -> None:
        private = "private@example.test"
        output = io.StringIO()
        with (
            patch.object(
                check_release_provenance,
                "read_remote_main_sha",
                return_value=SITE_SHA,
            ),
            patch.object(
                check_release_provenance,
                "github_api",
                side_effect=check_release_provenance.ProvenanceError(
                    "GitHub provenance API failed"
                ),
            ),
            patch.dict(
                check_release_provenance.os.environ,
                {"GITHUB_TOKEN": private},
            ),
            redirect_stdout(output),
        ):
            result = check_release_provenance.main(
                [
                    "--site-sha",
                    SITE_SHA,
                    "--ref",
                    check_release_provenance.MAIN_REF,
                ]
            )
        self.assertNotEqual(result, 0)
        self.assertNotIn(private, output.getvalue())


if __name__ == "__main__":
    unittest.main()
