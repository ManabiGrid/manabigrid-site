from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = ROOT / ".github" / "rulesets" / "main.proposal.json"


class MainRulesetProposalTests(unittest.TestCase):
    def test_proposal_is_non_bypassable_and_requires_exact_pr_check(self) -> None:
        payload = json.loads(PROPOSAL.read_text(encoding="utf-8"))
        self.assertEqual(payload["name"], "main")
        self.assertEqual(payload["target"], "branch")
        self.assertEqual(payload["enforcement"], "active")
        self.assertEqual(payload["bypass_actors"], [])
        self.assertEqual(
            payload["conditions"],
            {
                "ref_name": {
                    "exclude": [],
                    "include": ["~DEFAULT_BRANCH"],
                }
            },
        )
        rules = {rule["type"]: rule for rule in payload["rules"]}
        self.assertEqual(
            set(rules),
            {
                "deletion",
                "non_fast_forward",
                "pull_request",
                "required_status_checks",
            },
        )
        pull_request = rules["pull_request"]["parameters"]
        self.assertEqual(pull_request["required_approving_review_count"], 0)
        self.assertIs(pull_request["required_review_thread_resolution"], True)
        status = rules["required_status_checks"]["parameters"]
        self.assertIs(status["strict_required_status_checks_policy"], True)
        self.assertIs(status["do_not_enforce_on_create"], False)
        self.assertEqual(
            status["required_status_checks"],
            [
                {
                    "context": "manabigrid-site-pr-gate",
                    "integration_id": 15368,
                }
            ],
        )

    def test_rollback_document_keeps_application_outside_this_task(self) -> None:
        document = (ROOT / "MAIN_RULESET_PROPOSAL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("status: draft-not-applied", document)
        self.assertIn("今回は実行しない", document)
        self.assertIn("--method DELETE", document)
        self.assertIn("<作成応答のruleset-id>", document)
        self.assertIn("明示承認", document)
        self.assertIn("ruleset一覧が0件でない", document)
        self.assertIn("同じPOSTを再送しない", document)
        self.assertIn("X-GitHub-Api-Version: 2022-11-28", document)


if __name__ == "__main__":
    unittest.main()
