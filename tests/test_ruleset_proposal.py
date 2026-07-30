from __future__ import annotations

import json
from pathlib import Path
import unittest

import apply_ruleset


ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = ROOT / ".github" / "rulesets" / "main.proposal.json"
EXPECTED_RULE_TYPES = {
    "deletion",
    "non_fast_forward",
    "pull_request",
    "required_status_checks",
}
EXPECTED_PULL_REQUEST = {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews_on_push": False,
    "require_code_owner_review": False,
    "require_last_push_approval": False,
    "required_review_thread_resolution": True,
    "allowed_merge_methods": ["merge"],
}
EXPECTED_STATUS_CHECKS = {
    "strict_required_status_checks_policy": True,
    "do_not_enforce_on_create": False,
    "required_status_checks": [
        {
            "context": "manabigrid-site-pr-gate",
            "integration_id": 15368,
        }
    ],
}


def reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_proposal() -> dict:
    return json.loads(
        PROPOSAL.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_json_keys,
    )


def validate_json_exact(actual: object, expected: object, path: str = "$") -> None:
    if type(actual) is not type(expected):
        raise ValueError(
            f"{path}: expected {type(expected).__name__}, "
            f"got {type(actual).__name__}"
        )
    if isinstance(expected, dict):
        if set(actual) != set(expected):
            raise ValueError(f"{path}: object keys differ")
        for key in expected:
            validate_json_exact(actual[key], expected[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        if len(actual) != len(expected):
            raise ValueError(f"{path}: array length differs")
        for index, (actual_item, expected_item) in enumerate(
            zip(actual, expected)
        ):
            validate_json_exact(
                actual_item,
                expected_item,
                f"{path}[{index}]",
            )
        return
    if actual != expected:
        raise ValueError(f"{path}: value differs")


def validate_merge_only(payload: dict) -> None:
    pull_request_rules = [
        rule for rule in payload["rules"] if rule.get("type") == "pull_request"
    ]
    if len(pull_request_rules) != 1:
        raise ValueError("proposal must contain exactly one pull_request rule")
    methods = pull_request_rules[0]["parameters"].get("allowed_merge_methods")
    if methods != ["merge"]:
        raise ValueError("allowed_merge_methods must equal ['merge']")


def validate_exact_rule_types(payload: dict) -> None:
    rule_types = [rule.get("type") for rule in payload["rules"]]
    if any(not isinstance(rule_type, str) for rule_type in rule_types):
        raise ValueError("proposal rule types must be strings")
    if len(rule_types) != len(set(rule_types)):
        raise ValueError("proposal must not contain duplicate rule types")
    if set(rule_types) != EXPECTED_RULE_TYPES:
        raise ValueError("proposal must contain the exact reviewed rule types")


class MainRulesetProposalTests(unittest.TestCase):
    def test_proposal_has_no_explicit_bypass_and_requires_exact_pr_check(
        self,
    ) -> None:
        payload = load_proposal()
        self.assertEqual(payload, apply_ruleset.EXPECTED_PAYLOAD)
        self.assertEqual(payload["name"], "main")
        self.assertEqual(payload["target"], "branch")
        self.assertEqual(payload["enforcement"], "active")
        self.assertEqual(payload["bypass_actors"], [])
        self.assertEqual(
            set(payload),
            {
                "name",
                "target",
                "enforcement",
                "bypass_actors",
                "conditions",
                "rules",
            },
        )
        validate_json_exact(
            payload["conditions"],
            {
                "ref_name": {
                    "exclude": [],
                    "include": ["~DEFAULT_BRANCH"],
                }
            },
        )
        validate_exact_rule_types(payload)
        rules = {rule["type"]: rule for rule in payload["rules"]}
        self.assertEqual(rules["deletion"], {"type": "deletion"})
        self.assertEqual(
            rules["non_fast_forward"],
            {"type": "non_fast_forward"},
        )
        validate_json_exact(
            rules["pull_request"],
            {
                "type": "pull_request",
                "parameters": EXPECTED_PULL_REQUEST,
            },
            "$.rules.pull_request",
        )
        validate_merge_only(payload)
        validate_json_exact(
            rules["required_status_checks"],
            {
                "type": "required_status_checks",
                "parameters": EXPECTED_STATUS_CHECKS,
            },
            "$.rules.required_status_checks",
        )
        status = rules["required_status_checks"]["parameters"]
        self.assertEqual(
            status["required_status_checks"][0]["integration_id"],
            15368,
        )

    def test_merge_method_contract_rejects_any_non_exact_expansion(self) -> None:
        payload = load_proposal()
        pull_request = next(
            rule for rule in payload["rules"] if rule["type"] == "pull_request"
        )
        rejected_values = (
            ["merge", "squash"],
            ["merge", "rebase"],
            ["squash", "merge"],
            ["merge", "merge"],
            ["squash"],
            ["rebase"],
            [],
            "merge",
            {"merge": True},
            1,
            None,
        )
        for methods in rejected_values:
            with self.subTest(methods=methods):
                pull_request["parameters"]["allowed_merge_methods"] = methods
                with self.assertRaisesRegex(
                    ValueError,
                    r"allowed_merge_methods must equal \['merge'\]",
                ):
                    validate_merge_only(payload)

    def test_rule_type_contract_rejects_every_duplicate(self) -> None:
        payload = load_proposal()
        for duplicated in payload["rules"]:
            with self.subTest(rule_type=duplicated["type"]):
                candidate = {
                    **payload,
                    "rules": [*payload["rules"], dict(duplicated)],
                }
                with self.assertRaisesRegex(
                    ValueError,
                    "proposal must not contain duplicate rule types",
                ):
                    validate_exact_rule_types(candidate)

    def test_exact_contract_rejects_boolean_integer_aliases(self) -> None:
        aliases = (
            ("required_approving_review_count", False),
            ("dismiss_stale_reviews_on_push", 0),
            ("require_code_owner_review", 0),
            ("require_last_push_approval", 0),
            ("required_review_thread_resolution", 1),
        )
        for key, alias in aliases:
            with self.subTest(key=key, alias=alias):
                candidate = dict(EXPECTED_PULL_REQUEST)
                candidate[key] = alias
                with self.assertRaisesRegex(ValueError, "expected"):
                    validate_json_exact(
                        candidate,
                        EXPECTED_PULL_REQUEST,
                        "$.rules.pull_request.parameters",
                    )

    def test_exact_contract_rejects_unknown_rule_fields(self) -> None:
        candidates = (
            (
                {
                    "type": "pull_request",
                    "parameters": EXPECTED_PULL_REQUEST,
                    "unexpected_rule_field": True,
                },
                {
                    "type": "pull_request",
                    "parameters": EXPECTED_PULL_REQUEST,
                },
            ),
            (
                {
                    "type": "required_status_checks",
                    "parameters": EXPECTED_STATUS_CHECKS,
                    "unexpected_rule_field": True,
                },
                {
                    "type": "required_status_checks",
                    "parameters": EXPECTED_STATUS_CHECKS,
                },
            ),
        )
        for actual, expected in candidates:
            with self.subTest(rule_type=actual["type"]):
                with self.assertRaisesRegex(ValueError, "object keys differ"):
                    validate_json_exact(actual, expected, "$.rules")

    def test_strict_json_loader_rejects_nested_duplicate_keys(self) -> None:
        duplicate = (
            '{"rules":[{"type":"pull_request","parameters":{'
            '"allowed_merge_methods":["merge"],'
            '"allowed_merge_methods":["squash"]}}]}'
        )
        with self.assertRaisesRegex(
            ValueError,
            "duplicate JSON key: allowed_merge_methods",
        ):
            json.loads(
                duplicate,
                object_pairs_hook=reject_duplicate_json_keys,
            )

    def test_rollback_document_uses_only_the_guarded_entrypoint(self) -> None:
        document = (ROOT / "MAIN_RULESET_PROPOSAL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("status: draft-not-applied", document)
        self.assertIn("GitHubへはまだ適用していない", document)
        self.assertIn("python3 apply_ruleset.py preflight", document)
        self.assertIn("python3 apply_ruleset.py apply", document)
        self.assertIn("--approve-ruleset-application", document)
        self.assertIn("--approve-conditional-rollback", document)
        self.assertIn("明示承認", document)
        self.assertIn("ruleset一覧0件", document)
        self.assertIn("同じPOSTを再送せず", document)
        self.assertIn("STATE_UNKNOWN_AFTER_POST", document)
        self.assertIn("STATE_UNKNOWN_AFTER_DELETE", document)
        self.assertIn("所有IDを", document)
        self.assertIn("allowlist投影", document)
        self.assertIn("rules/branches/main", document)
        self.assertIn("--reviewed-payload-sha256", document)
        self.assertIn("updated_at", document)
        self.assertIn("guarded runner", document)
        self.assertIn("API version `2026-03-10`", document)
        self.assertNotIn("gh api --hostname", document)
        self.assertTrue((ROOT / "apply_ruleset.py").is_file())


if __name__ == "__main__":
    unittest.main()
