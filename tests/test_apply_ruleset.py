from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

import apply_ruleset


class StrictParsingTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_keys(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError) as context:
            apply_ruleset.strict_json_loads(
                '{"a":1,"a":2}',
                "duplicate test",
            )
        self.assertEqual(context.exception.code, "BLOCKED_SNAPSHOT_MALFORMED")

    def test_strict_json_rejects_utf8_bom(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.strict_json_loads("\ufeff{}", "bom test")

    def test_strict_json_rejects_nan_and_aliases(self) -> None:
        for raw in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(raw=raw):
                with self.assertRaises(apply_ruleset.RulesetGuardError):
                    apply_ruleset.strict_json_loads(raw, "constant test")

    def test_strict_json_rejects_trailing_data(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.strict_json_loads(
                '{"a":1} trailing',
                "trailing test",
            )

    def test_strict_json_rejects_empty_truncated_and_multiple_documents(
        self,
    ) -> None:
        for raw in ("", "   ", '{"a":', "{}\n[]"):
            with self.subTest(raw=raw):
                with self.assertRaises(apply_ruleset.RulesetGuardError):
                    apply_ruleset.strict_json_loads(raw, "shape test")


class PayloadAndPaginationTests(unittest.TestCase):
    def test_flatten_slurped_pages_accepts_empty_page(self) -> None:
        payload = "[[]]"
        self.assertEqual(apply_ruleset.flatten_slurped_pages(payload), [])

    def test_flatten_slurped_pages_rejects_non_array_root(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.flatten_slurped_pages("{}")

    def test_flatten_slurped_pages_rejects_non_array_page(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.flatten_slurped_pages('[{"id":1}]')

    def test_flatten_slurped_pages_rejects_bool_id(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.flatten_slurped_pages('[[{"id":true}]]')

    def test_flatten_slurped_pages_rejects_duplicate_ids(self) -> None:
        raw = "[[{\"id\":1},{\"id\":1}]]"
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.flatten_slurped_pages(raw)

    def test_flatten_slurped_pages_rejects_empty_outer_and_invalid_ids(
        self,
    ) -> None:
        for raw in (
            "[]",
            "[[],[]]",
            '[[{"id":1}],[]]',
            '[[{"id":0}]]',
            '[[{"id":-1}]]',
            '[[{"id":"1"}]]',
            '[[[{"id":1}]]]',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(apply_ruleset.RulesetGuardError):
                    apply_ruleset.flatten_slurped_pages(raw)

    def test_partial_page_output_with_nonzero_exit_is_never_accepted(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="[[{\"id\":1}]]",
            stderr="partial provider error",
        )
        with mock.patch.object(
            apply_ruleset,
            "run_command",
            return_value=completed,
        ):
            with self.assertRaises(apply_ruleset.ObservationUnavailable):
                apply_ruleset.api_get_ruleset_pages()

    def test_ruleset_inventory_is_not_filtered_to_one_target(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="[[]]",
            stderr="",
        )
        with mock.patch.object(
            apply_ruleset,
            "run_command",
            return_value=completed,
        ) as run:
            self.assertEqual(apply_ruleset.api_get_ruleset_pages(), [])
        command = run.call_args.args[0]
        self.assertIn("includes_parents=true", command)
        self.assertFalse(
            any(
                isinstance(argument, str)
                and argument.startswith("targets=")
                for argument in command
            )
        )


class IncludedResponseTests(unittest.TestCase):
    def test_parse_included_response_parses_status_201_204_404(self) -> None:
        for status, body in ("201", "{}"), ("204", ""), ("404", "{}"):
            with self.subTest(status=status):
                parsed = apply_ruleset.parse_included_response(
                    f"HTTP/2.0 {status} ok\nfoo: bar\n\n{body}",
                    "included",
                )
                self.assertEqual(parsed.status, int(status))

    def test_parse_included_response_rejects_malformed(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.parse_included_response(
                "no status\n\nbody",
                "bad",
            )

    def test_parse_included_response_rejects_multiple_status_blocks(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.parse_included_response(
                "HTTP/2.0 201 ok\nx: y\nHTTP/1.1 204 ok\n\n{}",
                "bad",
            )

    def test_parse_included_response_matches_gh_mixed_line_endings(self) -> None:
        parsed = apply_ruleset.parse_included_response(
            (
                "HTTP/2.0 201 Created\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                "X-Fixture: ok\r\n\r\n"
                '{"id":7}\n'
            ),
            "gh fixture",
        )
        self.assertEqual(parsed.status, 201)
        self.assertTrue(apply_ruleset.response_is_json(parsed))
        self.assertEqual(parsed.body, '{"id":7}\n')

    def test_json_response_requires_one_content_type_header(self) -> None:
        missing = apply_ruleset.parse_included_response(
            "HTTP/2.0 201 Created\nX-Fixture: ok\n\n{}",
            "missing content type",
        )
        duplicated = apply_ruleset.parse_included_response(
            (
                "HTTP/2.0 201 Created\n"
                "Content-Type: application/json\n"
                "Content-Type: application/json\n\n{}"
            ),
            "duplicate content type",
        )
        self.assertFalse(apply_ruleset.response_is_json(missing))
        self.assertFalse(apply_ruleset.response_is_json(duplicated))

    def test_json_media_type_rejects_prefix_lookalikes(self) -> None:
        for content_type in (
            "application/jsonp",
            "application/json-evil",
        ):
            with self.subTest(content_type=content_type):
                response = apply_ruleset.parse_included_response(
                    (
                        "HTTP/2.0 201 Created\n"
                        f"Content-Type: {content_type}\n\n"
                        "{}"
                    ),
                    "content type lookalike",
                )
                self.assertFalse(apply_ruleset.response_is_json(response))
        parameterized = apply_ruleset.parse_included_response(
            (
                "HTTP/2.0 201 Created\n"
                "Content-Type: application/json; charset=utf-8\n\n"
                "{}"
            ),
            "parameterized json",
        )
        self.assertTrue(apply_ruleset.response_is_json(parameterized))


class FreezeAndDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.proposal = Path(self.tmpdir.name) / "main.proposal.json"
        proposal = json.dumps(
            apply_ruleset.EXPECTED_PAYLOAD,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        self.proposal.write_text(proposal, encoding="utf-8")

        patcher = mock.patch.object(
            apply_ruleset,
            "PROPOSAL_PATH",
            self.proposal,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_freeze_payload_rejects_sha_drift(self) -> None:
        with self.assertRaises(apply_ruleset.RulesetGuardError) as context:
            apply_ruleset.freeze_payload("0" * 64)
        self.assertEqual(context.exception.code, "BLOCKED_INPUT_DRIFT")

    def test_freeze_payload_rejects_non_utf8(self) -> None:
        self.proposal.write_bytes(b"\xff\xfe\x00")
        sha = hashlib.sha256(self.proposal.read_bytes()).hexdigest()
        with self.assertRaises(apply_ruleset.RulesetGuardError) as context:
            apply_ruleset.freeze_payload(sha)
        self.assertEqual(context.exception.code, "BLOCKED_PAYLOAD_INVALID")

    def test_freeze_payload_rejects_malformed_contract(self) -> None:
        candidate = apply_ruleset.EXPECTED_PAYLOAD.copy()
        candidate["name"] = "different"
        self.proposal.write_text(json.dumps(candidate), encoding="utf-8")
        sha = hashlib.sha256(self.proposal.read_bytes()).hexdigest()
        with self.assertRaises(apply_ruleset.RulesetGuardError) as context:
            apply_ruleset.freeze_payload(sha)
        self.assertEqual(context.exception.code, "BLOCKED_PAYLOAD_INVALID")

    def test_verify_frozen_payload_unchanged_detects_toc_tou(self) -> None:
        expected_sha = hashlib.sha256(self.proposal.read_bytes()).hexdigest()
        frozen = apply_ruleset.freeze_payload(expected_sha)
        self.proposal.write_text("{}", encoding="utf-8")
        with self.assertRaises(apply_ruleset.RulesetGuardError) as context:
            apply_ruleset.verify_frozen_payload_unchanged(frozen)
        self.assertEqual(context.exception.code, "BLOCKED_DRIFT_BEFORE_POST")

    def test_pre_post_recheck_observes_local_payload_then_remote(self) -> None:
        expected_sha = hashlib.sha256(self.proposal.read_bytes()).hexdigest()
        frozen = apply_ruleset.freeze_payload(expected_sha)
        reviewed_sha = "a" * 40
        baseline = {"site_sha": reviewed_sha}
        events: list[str] = []

        def verify_local(_reviewed_sha: str) -> None:
            events.append("local")

        def verify_payload(_payload: apply_ruleset.FrozenPayload) -> None:
            events.append("payload")

        def capture(
            _reviewed_sha: str,
            *,
            now: datetime | None = None,
        ) -> dict[str, Any]:
            del now
            events.append("remote")
            return baseline

        with (
            mock.patch.object(
                apply_ruleset,
                "verify_local_release",
                side_effect=verify_local,
            ),
            mock.patch.object(
                apply_ruleset,
                "verify_frozen_payload_unchanged",
                side_effect=verify_payload,
            ),
            mock.patch.object(
                apply_ruleset,
                "capture_remote_snapshot",
                side_effect=capture,
            ),
        ):
            apply_ruleset.recheck_before_post(
                reviewed_sha,
                frozen,
                baseline,
            )
        self.assertEqual(events, ["local", "payload", "remote"])


class JournalTests(unittest.TestCase):
    def test_journal_is_owner_only_and_appends_state_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / "review/transaction.json"
            with mock.patch.object(
                apply_ruleset,
                "JOURNAL_PATH",
                journal,
            ):
                apply_ruleset.write_new_journal(
                    {
                        "schema_version": 1,
                        "transaction_id": "fixture",
                        "events": [
                            {
                                "state": "POST_INTENT_RECORDED",
                                "at": "2026-07-30T00:00:00+00:00",
                                "codes": [],
                            }
                        ],
                    }
                )
                apply_ruleset.update_journal(
                    "fixture",
                    "STATE_UNKNOWN_AFTER_POST",
                    codes=("DO_NOT_RETRY_POST",),
                    now=datetime(
                        2026,
                        7,
                        30,
                        0,
                        1,
                        tzinfo=timezone.utc,
                    ),
                )
            payload = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(
                [event["state"] for event in payload["events"]],
                [
                    "POST_INTENT_RECORDED",
                    "STATE_UNKNOWN_AFTER_POST",
                ],
            )
            self.assertEqual(journal.stat().st_mode & 0o777, 0o600)


class ResponseProjectionTests(unittest.TestCase):
    def _detail_response(self) -> dict[str, Any]:
        return {
            **json.loads(json.dumps(apply_ruleset.EXPECTED_PAYLOAD)),
            "id": 12,
            "source_type": "Repository",
            "source": apply_ruleset.REPOSITORY,
            "created_at": "2026-07-30T00:00:00Z",
            "updated_at": "2026-07-30T00:00:01Z",
            "current_user_can_bypass": "never",
        }

    def test_project_response_allows_top_level_server_fields(self) -> None:
        response = {
            **json.loads(json.dumps(apply_ruleset.EXPECTED_PAYLOAD)),
            "id": 1,
            "node_id": "fixture",
            "source_type": "Repository",
            "source": apply_ruleset.REPOSITORY,
            "created_at": "2026-07-30T00:00:00Z",
            "updated_at": "2026-07-30T00:00:01Z",
            "current_user_can_bypass": "never",
            "_links": {"self": {"href": "https://example.invalid"}},
        }
        projected = apply_ruleset.project_response_payload(response)
        self.assertEqual(
            set(projected["rules"][0]),
            set(apply_ruleset.EXPECTED_PAYLOAD["rules"][0].keys()),
        )

    def test_project_response_rejects_unknown_top_level_field(self) -> None:
        response = {
            **json.loads(json.dumps(apply_ruleset.EXPECTED_PAYLOAD)),
            "unexpected_behavior": True,
        }
        with self.assertRaises(apply_ruleset.RulesetGuardError) as raised:
            apply_ruleset.project_response_payload(response)
        self.assertEqual(raised.exception.code, "POSTFLIGHT_MISMATCH")

    def test_project_response_allows_only_exact_optional_noop_defaults(
        self,
    ) -> None:
        response = json.loads(json.dumps(apply_ruleset.EXPECTED_PAYLOAD))
        pull_request = next(
            rule
            for rule in response["rules"]
            if rule["type"] == "pull_request"
        )
        pull_request["parameters"]["dismissal_restriction"] = {
            "enabled": False,
            "allowed_actors": [],
        }
        pull_request["parameters"]["required_reviewers"] = []
        projected = apply_ruleset.project_response_payload(response)
        self.assertTrue(apply_ruleset.payload_projection_matches(projected))

    def test_project_response_rejects_unknown_rule_level_field(self) -> None:
        response = json.loads(json.dumps(apply_ruleset.EXPECTED_PAYLOAD))
        response["rules"][0]["extra"] = "unknown"
        with self.assertRaises(apply_ruleset.RulesetGuardError) as raised:
            apply_ruleset.project_response_payload(response)
        self.assertEqual(raised.exception.code, "POSTFLIGHT_MISMATCH")

    def test_project_response_rejects_request_missing_rule_type(self) -> None:
        response = dict(
            apply_ruleset.EXPECTED_PAYLOAD,
            id=1,
            rules=[{"type": "deletion"}],
        )
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.project_response_payload(response)

    def test_project_response_rejects_duplicate_rule_types(self) -> None:
        response = dict(
            apply_ruleset.EXPECTED_PAYLOAD,
            id=1,
            rules=[*apply_ruleset.EXPECTED_PAYLOAD["rules"]] * 2,
        )
        with self.assertRaises(apply_ruleset.RulesetGuardError):
            apply_ruleset.project_response_payload(response)

    def test_project_response_accepts_rule_reorder(
        self,
    ) -> None:
        rules = []
        for rule in reversed(apply_ruleset.EXPECTED_PAYLOAD["rules"]):
            candidate = json.loads(json.dumps(rule))
            rules.append(candidate)
        response = {
            **apply_ruleset.EXPECTED_PAYLOAD,
            "rules": rules,
        }
        projected = apply_ruleset.project_response_payload(response)
        self.assertTrue(apply_ruleset.payload_projection_matches(projected))

    def test_project_response_rejects_unknown_nested_behavior_field(
        self,
    ) -> None:
        extras = (
            ("server_default", False),
            (
                "dismissal_restriction",
                {"enabled": True, "allowed_actors": []},
            ),
            (
                "dismissal_restriction",
                {"enabled": False, "allowed_actors": ["unexpected"]},
            ),
            (
                "dismissal_restriction",
                {"enabled": 0, "allowed_actors": []},
            ),
            (
                "dismissal_restriction",
                {"enabled": 1, "allowed_actors": []},
            ),
            ("required_reviewers", [{"minimum_approvals": 1}]),
        )
        for index, (key, value) in enumerate(extras):
            with self.subTest(key=key, case=index):
                response = json.loads(
                    json.dumps(apply_ruleset.EXPECTED_PAYLOAD)
                )
                pull_request = next(
                    rule
                    for rule in response["rules"]
                    if rule["type"] == "pull_request"
                )
                pull_request["parameters"][key] = value
                with self.assertRaises(
                    apply_ruleset.RulesetGuardError
                ) as raised:
                    apply_ruleset.project_response_payload(response)
                self.assertEqual(
                    raised.exception.code,
                    "POSTFLIGHT_MISMATCH",
                )

    def test_project_response_rejects_boolean_integer_alias(self) -> None:
        response = json.loads(json.dumps(apply_ruleset.EXPECTED_PAYLOAD))
        pull_request = next(
            rule
            for rule in response["rules"]
            if rule["type"] == "pull_request"
        )
        pull_request["parameters"]["required_approving_review_count"] = False
        with self.assertRaises(apply_ruleset.RulesetGuardError) as raised:
            apply_ruleset.project_response_payload(response)
        self.assertEqual(raised.exception.code, "POSTFLIGHT_MISMATCH")

    def test_detail_preserves_semantic_mismatch_for_bounded_rollback(
        self,
    ) -> None:
        response = self._detail_response()
        pull_request = next(
            rule
            for rule in response["rules"]
            if rule["type"] == "pull_request"
        )
        pull_request["parameters"]["allowed_merge_methods"] = [
            "merge",
            "squash",
        ]
        detail = apply_ruleset.extract_ruleset_detail(response)
        self.assertEqual(detail.projection_state, "mismatch")
        self.assertFalse(detail.payload_matches)
        self.assertIsNone(detail.projected_payload)
        self.assertRegex(detail.config_sha256, r"^[0-9a-f]{64}$")

    def test_unknown_top_level_value_drift_changes_detail_identity(
        self,
    ) -> None:
        first_response = self._detail_response()
        second_response = self._detail_response()
        first_response["unexpected_behavior"] = "A"
        second_response["unexpected_behavior"] = "B"
        first = apply_ruleset.extract_ruleset_detail(first_response)
        second = apply_ruleset.extract_ruleset_detail(second_response)
        self.assertEqual(first.projection_state, "mismatch")
        self.assertEqual(second.projection_state, "mismatch")
        self.assertNotEqual(first.config_sha256, second.config_sha256)
        self.assertFalse(apply_ruleset.compare_detail_identity(first, second))

    def test_postflight_never_accepts_a_bypassable_ruleset(self) -> None:
        response = self._detail_response()
        response["current_user_can_bypass"] = "always"
        detail = apply_ruleset.extract_ruleset_detail(response)
        ruleset_id = detail.ruleset_id
        summary = {
            "id": ruleset_id,
            "name": detail.name,
            "target": "branch",
            "source_type": detail.source_type,
            "source": detail.source,
            "enforcement": "active",
            "created_at": detail.created_at,
            "updated_at": detail.updated_at,
        }
        effective_rules = [
            {
                "ruleset_id": ruleset_id,
                "ruleset_source_type": "Repository",
                "ruleset_source": apply_ruleset.REPOSITORY,
                **json.loads(json.dumps(rule)),
            }
            for rule in apply_ruleset.EXPECTED_PAYLOAD["rules"]
        ]
        baseline = {
            "site_sha": "a" * 40,
            "repository": {
                "full_name": apply_ruleset.REPOSITORY,
                "default_branch": "main",
            },
            "rulesets": [],
            "effective_rules": [],
            "operational": {},
        }
        observed = {
            **baseline,
            "rulesets": [summary],
            "effective_rules": effective_rules,
        }
        created = apply_ruleset.CreatedReference(
            ruleset_id=ruleset_id,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "capture_remote_snapshot",
                return_value=observed,
            ),
            mock.patch.object(
                apply_ruleset,
                "load_ruleset_detail",
                return_value=detail,
            ),
        ):
            assessment = apply_ruleset.assess_postflight(
                baseline,
                created,
                baseline["site_sha"],
            )
        self.assertEqual(assessment.state, "POSTFLIGHT_MISMATCH")
        self.assertEqual(assessment.codes, ("DETAIL_BYPASS_MISMATCH",))

    def test_detail_missing_request_field_is_unverified_not_mismatch(
        self,
    ) -> None:
        response = self._detail_response()
        del response["conditions"]
        detail = apply_ruleset.extract_ruleset_detail(response)
        self.assertEqual(detail.projection_state, "omitted")
        self.assertFalse(detail.payload_matches)


class EffectiveProjectionTests(unittest.TestCase):
    def _effective_rule(self, ruleset_id: int) -> list[dict[str, Any]]:
        rule_templates = {
            rule["type"]: rule for rule in apply_ruleset.EXPECTED_PAYLOAD["rules"]
        }
        return [
            {
                "ruleset_id": ruleset_id,
                "type": rule_type,
                "ruleset_source_type": "Repository",
                "ruleset_source": apply_ruleset.REPOSITORY,
                **json.loads(json.dumps(rule)),
            }
            for rule_type, rule in rule_templates.items()
        ]

    def test_expected_effective_projection_accepts_semantically_equivalent(self) -> None:
        rules = self._effective_rule(7)
        pull_request = next(
            rule for rule in rules if rule["type"] == "pull_request"
        )
        pull_request["parameters"]["dismissal_restriction"] = {
            "enabled": False,
            "allowed_actors": [],
        }
        pull_request["parameters"]["required_reviewers"] = []
        projected, codes = apply_ruleset.expected_effective_projection(
            rules,
            7,
        )
        self.assertIsNotNone(projected)
        self.assertEqual(codes, tuple())

    def test_effective_projection_rejects_non_noop_optional_constraint(
        self,
    ) -> None:
        rules = self._effective_rule(7)
        pull_request = next(
            rule for rule in rules if rule["type"] == "pull_request"
        )
        pull_request["parameters"]["dismissal_restriction"] = {
            "enabled": 0,
            "allowed_actors": [],
        }
        projected, codes = apply_ruleset.expected_effective_projection(
            rules,
            7,
        )
        self.assertIsNone(projected)
        self.assertEqual(codes, ("EFFECTIVE_RULE_SEMANTIC_MISMATCH",))

    def test_expected_effective_projection_rejects_wrong_ids(self) -> None:
        _, codes = apply_ruleset.expected_effective_projection(
            self._effective_rule(7) + self._effective_rule(8),
            7,
        )
        self.assertIn("CONCURRENT_EFFECTIVE_RULESET", codes)

    def test_expected_effective_projection_rejects_wrong_source(self) -> None:
        bad = self._effective_rule(7)
        bad[0]["ruleset_source"] = "bad"
        _, codes = apply_ruleset.expected_effective_projection(bad, 7)
        self.assertIn("EFFECTIVE_RULE_SOURCE_MISMATCH", codes)

    def test_expected_effective_projection_rejects_type_mismatch(self) -> None:
        bad = self._effective_rule(7)
        bad[0]["type"] = "missing"
        _, codes = apply_ruleset.expected_effective_projection(bad, 7)
        self.assertIn("EFFECTIVE_RULE_SET_INCOMPLETE", codes)

    def test_expected_effective_projection_detects_parameter_mismatch(
        self,
    ) -> None:
        bad = self._effective_rule(7)
        pull_request = next(
            rule for rule in bad if rule["type"] == "pull_request"
        )
        pull_request["parameters"]["allowed_merge_methods"] = [
            "merge",
            "squash",
        ]
        _, codes = apply_ruleset.expected_effective_projection(bad, 7)
        self.assertEqual(
            codes,
            ("EFFECTIVE_RULE_SEMANTIC_MISMATCH",),
        )

    def test_effective_rules_reject_wrong_check_or_integration(self) -> None:
        for key, value in (
            ("context", "shadow-gate"),
            ("integration_id", 999),
        ):
            bad = self._effective_rule(7)
            status = next(
                rule
                for rule in bad
                if rule["type"] == "required_status_checks"
            )
            status["parameters"]["required_status_checks"][0][key] = value
            _, codes = apply_ruleset.expected_effective_projection(bad, 7)
            with self.subTest(key=key):
                self.assertEqual(
                    codes,
                    ("EFFECTIVE_RULE_SEMANTIC_MISMATCH",),
                )


class AttemptCreateAndRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.reviewed_sha = "a" * 40
        self.payload_text = json.dumps(
            apply_ruleset.EXPECTED_PAYLOAD,
            ensure_ascii=False,
            sort_keys=True,
        )
        self.frozen = apply_ruleset.FrozenPayload(
            raw_bytes=self.payload_text.encode("utf-8"),
            text=self.payload_text,
            sha256=hashlib.sha256(self.payload_text.encode("utf-8")).hexdigest(),
            decoded=apply_ruleset.EXPECTED_PAYLOAD,
        )
        self.baseline = {
            "site_sha": self.reviewed_sha,
            "repository": {
                "full_name": apply_ruleset.REPOSITORY,
                "default_branch": "main",
            },
            "rulesets": [],
            "effective_rules": [],
            "operational": {},
        }
        self.journal = Path(self.tmpdir.name) / "review/ruleset-apply-transaction.json"
        self.real_write_new_journal = apply_ruleset.write_new_journal

        p1 = mock.patch.object(apply_ruleset, "freeze_payload", return_value=self.frozen)
        p2 = mock.patch.object(apply_ruleset, "perform_preflight", return_value=self.baseline)
        p3 = mock.patch.object(apply_ruleset, "recheck_before_post", return_value=None)
        p4 = mock.patch.object(apply_ruleset, "write_new_journal", return_value=None)
        p5 = mock.patch.object(apply_ruleset, "update_journal", return_value=None)
        p6 = mock.patch.object(apply_ruleset, "JOURNAL_PATH", self.journal)
        p6.start(); self.addCleanup(p6.stop)
        p1.start(); self.addCleanup(p1.stop)
        p2.start(); self.addCleanup(p2.stop)
        p3.start(); self.addCleanup(p3.stop)
        p4.start(); self.addCleanup(p4.stop)
        p5.start(); self.addCleanup(p5.stop)

    def test_create_command_invalid_status_becomes_unknown_and_no_retry(self) -> None:
        post = mock.patch.object(
            apply_ruleset,
            "run_command",
            return_value=mock.Mock(
                returncode=0,
                stdout=(
                    "HTTP/2.0 200 ok\n"
                    "Content-Type: application/json\n\n{}"
                ),
            ),
        )
        with post:
            with self.assertRaises(apply_ruleset.MutationCommandUncertain):
                # direct API: wrong status should be treated as uncertain
                apply_ruleset.attempt_create(self.frozen)

    def test_create_uses_exact_frozen_bytes_once_and_never_payload_path(
        self,
    ) -> None:
        response = (
            "HTTP/2.0 201 Created\n"
            "Content-Type: application/json\n\n"
            '{"id":12}'
        )
        with mock.patch.object(
            apply_ruleset,
            "run_command",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=response,
                stderr="",
            ),
        ) as run:
            created = apply_ruleset.attempt_create(self.frozen)
        self.assertEqual(created.ruleset_id, 12)
        self.assertEqual(created.projection_state, "omitted")
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs["input_bytes"], self.frozen.raw_bytes)
        self.assertEqual(command[-2:], ["--input", "-"])
        self.assertNotIn(str(apply_ruleset.PROPOSAL_PATH), command)

    def test_create_unknown_cases_never_retry_or_relay_provider_body(
        self,
    ) -> None:
        private = "private-fixture@example.test"
        cases = (
            subprocess.TimeoutExpired(cmd=["gh"], timeout=30),
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=(
                    "HTTP/2.0 422 Unprocessable Entity\n"
                    "Content-Type: application/json\n\n"
                    f'{{"message":"{private}"}}'
                ),
                stderr=private,
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    "HTTP/2.0 201 Created\n"
                    "Content-Type: application/json\n\n"
                    '{"id":12} trailing'
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    "HTTP/2.0 201 Created\n"
                    "Content-Type: application/json\n\n"
                    '{"id":true}'
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="HTTP/2.0 201 Created\nX-Fixture: ok\n\n{\"id\":12}",
                stderr="",
            ),
        )
        for outcome in cases:
            with self.subTest(outcome=type(outcome).__name__):
                patch = (
                    mock.patch.object(
                        apply_ruleset,
                        "run_command",
                        side_effect=outcome,
                    )
                    if isinstance(outcome, BaseException)
                    else mock.patch.object(
                        apply_ruleset,
                        "run_command",
                        return_value=outcome,
                    )
                )
                with patch as run:
                    with self.assertRaises(
                        apply_ruleset.MutationCommandUncertain
                    ) as raised:
                        apply_ruleset.attempt_create(self.frozen)
                self.assertEqual(run.call_count, 1)
                self.assertNotIn(private, str(raised.exception))

    def test_delete_requires_one_exact_empty_204(self) -> None:
        valid = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="HTTP/2.0 204 No Content\nX-Fixture: ok\n\n",
            stderr="",
        )
        with mock.patch.object(
            apply_ruleset,
            "run_command",
            return_value=valid,
        ) as run:
            response = apply_ruleset.attempt_delete(12)
        self.assertEqual(response.status, 204)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("DELETE", command)
        self.assertEqual(
            command[-1],
            f"repos/{apply_ruleset.REPOSITORY}/rulesets/12",
        )

    def test_delete_unknown_cases_never_retry(self) -> None:
        cases = (
            subprocess.TimeoutExpired(cmd=["gh"], timeout=30),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="HTTP/2.0 204 No Content\nX: y\n\n{}",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="HTTP/2.0 204 No Content\nX: y\n\n   \n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="HTTP/2.0 200 OK\nContent-Type: application/json\n\n{}",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="HTTP/2.0 500 Error\nContent-Type: application/json\n\n{}",
                stderr="provider error",
            ),
        )
        for outcome in cases:
            with self.subTest(outcome=type(outcome).__name__):
                patch = (
                    mock.patch.object(
                        apply_ruleset,
                        "run_command",
                        side_effect=outcome,
                    )
                    if isinstance(outcome, BaseException)
                    else mock.patch.object(
                        apply_ruleset,
                        "run_command",
                        return_value=outcome,
                    )
                )
                with patch as run:
                    with self.assertRaises(
                        apply_ruleset.MutationCommandUncertain
                    ):
                        apply_ruleset.attempt_delete(12)
                self.assertEqual(run.call_count, 1)

    def test_delete_rejects_bool_zero_and_negative_id_before_command(
        self,
    ) -> None:
        for ruleset_id in (True, 0, -1):
            with self.subTest(ruleset_id=ruleset_id):
                with mock.patch.object(
                    apply_ruleset,
                    "run_command",
                ) as run:
                    with self.assertRaises(
                        apply_ruleset.RulesetGuardError
                    ):
                        apply_ruleset.attempt_delete(ruleset_id)
                run.assert_not_called()

    def test_apply_guarded_missing_approval_flags_blocked(self) -> None:
        code, report = apply_ruleset.apply_guarded(
            self.reviewed_sha,
            self.frozen.sha256,
            approve_application=False,
            approve_conditional_rollback=False,
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["state"], "BLOCKED_AUTHORIZATION")

    def test_apply_guarded_journal_exists_blocks_before_mutation(self) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_text("{}", encoding="utf-8")
        p = mock.patch.object(apply_ruleset, "JOURNAL_PATH", self.journal)
        with p:
            now = lambda: datetime(2026, 7, 30, 0, 2, tzinfo=timezone.utc)
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 2)
        self.assertEqual(report["state"], "BLOCKED_AUTHORIZATION")

    def test_apply_guarded_unknown_post_does_not_delete(self) -> None:
        with mock.patch.object(
            apply_ruleset,
            "attempt_create",
            side_effect=apply_ruleset.MutationCommandUncertain("state"),
        ):
            with mock.patch.object(apply_ruleset, "attempt_delete") as delete:
                code, report = apply_ruleset.apply_guarded(
                    self.reviewed_sha,
                    self.frozen.sha256,
                    approve_application=True,
                    approve_conditional_rollback=True,
                )
        self.assertEqual(code, 3)
        self.assertEqual(report["state"], "STATE_UNKNOWN_AFTER_POST")
        self.assertEqual(report["mutation_state"], "unknown")
        self.assertEqual(delete.call_count, 0)

    def test_parent_directory_fsync_failure_blocks_before_post(self) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        failure = apply_ruleset.RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            "fixture directory fsync failure",
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "write_new_journal",
                side_effect=self.real_write_new_journal,
            ),
            mock.patch.object(
                apply_ruleset,
                "fsync_directory",
                side_effect=failure,
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
            ) as post,
        ):
            now = lambda: datetime(
                2026,
                7,
                30,
                0,
                0,
                tzinfo=timezone.utc,
            )
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 2)
        self.assertEqual(report["state"], "BLOCKED_AUTHORIZATION")
        self.assertEqual(report["mutation_state"], "none")
        post.assert_not_called()

    def test_post_window_is_rechecked_after_durable_intent(self) -> None:
        post_intent = datetime(
            2026,
            7,
            30,
            0,
            0,
            tzinfo=timezone.utc,
        )
        clock_values = iter(
            [
                post_intent,
                post_intent,
                post_intent,
                post_intent + timedelta(minutes=15, seconds=1),
            ]
        )
        with mock.patch.object(
            apply_ruleset,
            "attempt_create",
        ) as post:
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=lambda: next(clock_values),
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            report["codes"],
            ["POST_WINDOW_EXPIRED_OR_CLOCK_REVERSED"],
        )
        self.assertEqual(report["mutation_state"], "none")
        post.assert_not_called()

    def test_remote_drift_after_durable_intent_blocks_post(self) -> None:
        drift = apply_ruleset.RulesetGuardError(
            "BLOCKED_DRIFT_BEFORE_POST",
            "fixture remote drift after journal fsync",
        )
        events: list[str] = []

        def recheck(*_args: object, **_kwargs: object) -> None:
            events.append("recheck")
            if events.count("recheck") == 2:
                raise drift

        def write_journal(_record: dict[str, Any]) -> None:
            events.append("journal")

        with (
            mock.patch.object(
                apply_ruleset,
                "recheck_before_post",
                side_effect=recheck,
            ) as recheck_mock,
            mock.patch.object(
                apply_ruleset,
                "write_new_journal",
                side_effect=write_journal,
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
            ) as post,
        ):
            now = lambda: datetime(
                2026,
                7,
                30,
                0,
                0,
                tzinfo=timezone.utc,
            )
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 1)
        self.assertEqual(report["state"], "BLOCKED_DRIFT_BEFORE_POST")
        self.assertEqual(report["mutation_state"], "none")
        self.assertEqual(recheck_mock.call_count, 2)
        self.assertEqual(events, ["recheck", "journal", "recheck"])
        post.assert_not_called()

    def test_monotonic_post_window_expiry_blocks_before_post(self) -> None:
        now = lambda: datetime(
            2026,
            7,
            30,
            0,
            0,
            tzinfo=timezone.utc,
        )
        monotonic_values = iter([0.0, 901.0])
        with mock.patch.object(
            apply_ruleset,
            "attempt_create",
        ) as post:
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
                monotonic_function=lambda: next(monotonic_values),
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            report["codes"],
            ["POST_WINDOW_EXPIRED_OR_CLOCK_REVERSED"],
        )
        post.assert_not_called()

    def test_journal_failure_after_create_stops_before_postflight_or_delete(
        self,
    ) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=None,
            projection_state="omitted",
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "journal_transition",
                return_value=False,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
            ) as assess,
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
            ) as delete,
        ):
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
            )
        self.assertEqual(code, 3)
        self.assertEqual(report["codes"], ["JOURNAL_UPDATE_FAILED"])
        assess.assert_not_called()
        delete.assert_not_called()

    def test_verified_create_is_not_success_when_terminal_journal_fails(
        self,
    ) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        verified = apply_ruleset.PostflightAssessment(
            state="APPLIED_CONFIG_VERIFIED_BEHAVIOR_PENDING",
            codes=(),
            snapshot=dict(self.baseline),
            detail=None,
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                return_value=verified,
            ),
            mock.patch.object(
                apply_ruleset,
                "journal_transition",
                side_effect=[True, False],
            ),
        ):
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
            )
        self.assertEqual(code, 3)
        self.assertEqual(report["state"], "JOURNAL_INCOMPLETE_AFTER_CREATE")
        self.assertEqual(report["mutation_state"], "confirmed_created")
        self.assertEqual(report["codes"], ["JOURNAL_UPDATE_FAILED"])

    def test_apply_guarded_postflight_unavailable_no_rollback(self) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=None,
            projection_state="exact",
        )
        with mock.patch.object(
            apply_ruleset,
            "attempt_create",
            return_value=created,
        ), mock.patch.object(
            apply_ruleset,
            "assess_postflight",
            return_value=apply_ruleset.PostflightAssessment(
                state="POSTFLIGHT_UNVERIFIED",
                codes=("OBSERVATION_UNAVAILABLE",),
                snapshot=None,
                detail=None,
            ),
        ):
            with mock.patch.object(apply_ruleset, "attempt_delete") as delete:
                code, report = apply_ruleset.apply_guarded(
                    self.reviewed_sha,
                    self.frozen.sha256,
                    approve_application=True,
                    approve_conditional_rollback=True,
                )
        self.assertEqual(code, 3)
        self.assertEqual(report["state"], "POSTFLIGHT_UNVERIFIED")
        self.assertEqual(delete.call_count, 0)

    def test_unexpected_postflight_failure_is_redacted_and_never_rolls_back(
        self,
    ) -> None:
        private = "private-provider-body@example.test"
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=None,
            projection_state="omitted",
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                side_effect=RuntimeError(private),
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
            ) as delete,
        ):
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
            )
        self.assertEqual(code, 3)
        self.assertEqual(report["codes"], ["INTERNAL_POSTFLIGHT_FAILURE"])
        self.assertNotIn(private, json.dumps(report))
        delete.assert_not_called()

    def test_apply_guarded_conditional_rollback_requires_double_same_mismatch(self) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        mismatch = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline, effective_rules=[{"x": 1}]),
            detail=apply_ruleset.RulesetDetail(
                ruleset_id=12,
                name="main",
                source_type="Repository",
                source=apply_ruleset.REPOSITORY,
                created_at="2026-07-30T00:00:00Z",
                updated_at="2026-07-30T00:00:01Z",
                current_user_can_bypass="must not bypass",
                projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
                payload_matches=True,
            ),
        )
        with mock.patch.object(apply_ruleset, "attempt_create", return_value=created), \
             mock.patch.object(apply_ruleset, "assess_postflight", side_effect=[mismatch, mismatch, mismatch]), \
             mock.patch.object(
                apply_ruleset,
                "attempt_delete",
                return_value=apply_ruleset.IncludedResponse(status=204, body=""),
             ), \
             mock.patch.object(
                apply_ruleset,
                "verify_baseline_restored",
                return_value=True,
             ):
            now = lambda: datetime(2026, 7, 30, 0, 2, tzinfo=timezone.utc)
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 4)
        self.assertEqual(report["state"], "ROLLED_BACK_VERIFIED")
        self.assertNotIn("DO_NOT_RETRY_DELETE", report["codes"])

    def test_apply_guarded_rollback_precondition_drift_blocks_delete(self) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        mismatch = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=apply_ruleset.RulesetDetail(
                ruleset_id=12,
                name="main",
                source_type="Repository",
                source=apply_ruleset.REPOSITORY,
                created_at="2026-07-30T00:00:00Z",
                updated_at="2026-07-30T00:00:01Z",
                current_user_can_bypass="must not bypass",
                projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
                payload_matches=True,
            ),
        )
        different = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=apply_ruleset.RulesetDetail(
                ruleset_id=13,
                name="main",
                source_type="Repository",
                source=apply_ruleset.REPOSITORY,
                created_at="2026-07-30T00:00:00Z",
                updated_at="2026-07-30T00:00:01Z",
                current_user_can_bypass="must not bypass",
                projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
                payload_matches=True,
            ),
        )
        with mock.patch.object(apply_ruleset, "attempt_create", return_value=created), \
             mock.patch.object(apply_ruleset, "assess_postflight", side_effect=[mismatch, different]), \
             mock.patch.object(apply_ruleset, "attempt_delete") as delete:
            now = lambda: datetime(2026, 7, 30, 0, 2, tzinfo=timezone.utc)
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 1)
        self.assertEqual(report["state"], "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE")
        self.assertEqual(report["codes"], ["ROLLBACK_PRECONDITION_DRIFT"])
        self.assertEqual(delete.call_count, 0)

    def test_same_id_timestamp_or_config_drift_blocks_delete(self) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        original_detail = apply_ruleset.RulesetDetail(
            ruleset_id=12,
            name="main",
            source_type="Repository",
            source=apply_ruleset.REPOSITORY,
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:01Z",
            current_user_can_bypass="never",
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            payload_matches=False,
            projection_state="mismatch",
            config_sha256="a" * 64,
        )
        original = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=original_detail,
        )
        drifted_details = (
            apply_ruleset.RulesetDetail(
                **{
                    **original_detail.__dict__,
                    "updated_at": "2026-07-30T00:00:02Z",
                }
            ),
            apply_ruleset.RulesetDetail(
                **{
                    **original_detail.__dict__,
                    "config_sha256": "b" * 64,
                }
            ),
        )
        for drifted in drifted_details:
            with self.subTest(drift=drifted.identity_marker()):
                second = apply_ruleset.PostflightAssessment(
                    state="POSTFLIGHT_MISMATCH",
                    codes=original.codes,
                    snapshot=dict(self.baseline),
                    detail=drifted,
                )
                with (
                    mock.patch.object(
                        apply_ruleset,
                        "attempt_create",
                        return_value=created,
                    ),
                    mock.patch.object(
                        apply_ruleset,
                        "assess_postflight",
                        side_effect=[original, second],
                    ),
                    mock.patch.object(
                        apply_ruleset,
                        "attempt_delete",
                    ) as delete,
                ):
                    now = lambda: datetime(
                        2026,
                        7,
                        30,
                        0,
                        2,
                        tzinfo=timezone.utc,
                    )
                    code, report = apply_ruleset.apply_guarded(
                        self.reviewed_sha,
                        self.frozen.sha256,
                        approve_application=True,
                        approve_conditional_rollback=True,
                        now_function=now,
                    )
                self.assertEqual(code, 1)
                self.assertEqual(
                    report["codes"],
                    ["ROLLBACK_PRECONDITION_DRIFT"],
                )
                delete.assert_not_called()

    def test_same_mismatch_code_with_changed_snapshot_blocks_delete(
        self,
    ) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        detail = apply_ruleset.RulesetDetail(
            ruleset_id=12,
            name="main",
            source_type="Repository",
            source=apply_ruleset.REPOSITORY,
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:01Z",
            current_user_can_bypass="never",
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            payload_matches=False,
            projection_state="mismatch",
            config_sha256="a" * 64,
        )
        first = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("EFFECTIVE_RULE_SEMANTIC_MISMATCH",),
            snapshot={
                **self.baseline,
                "effective_rules": [{"parameters": {"value": 1}}],
            },
            detail=detail,
        )
        second = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=first.codes,
            snapshot={
                **self.baseline,
                "effective_rules": [{"parameters": {"value": 2}}],
            },
            detail=detail,
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                side_effect=[first, second],
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
            ) as delete,
        ):
            now = lambda: datetime(
                2026,
                7,
                30,
                0,
                2,
                tzinfo=timezone.utc,
            )
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 1)
        self.assertEqual(report["codes"], ["ROLLBACK_PRECONDITION_DRIFT"])
        delete.assert_not_called()

    def test_drift_after_delete_intent_is_durable_blocks_delete(self) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        detail = apply_ruleset.RulesetDetail(
            ruleset_id=12,
            name="main",
            source_type="Repository",
            source=apply_ruleset.REPOSITORY,
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:01Z",
            current_user_can_bypass="never",
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            payload_matches=False,
            projection_state="mismatch",
            config_sha256="a" * 64,
        )
        stable = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=detail,
        )
        drifted = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=stable.codes,
            snapshot=dict(self.baseline, effective_rules=[{"drift": True}]),
            detail=detail,
        )
        events: list[str] = []
        assessments = iter([stable, stable, drifted])

        def assess(*_args: object, **_kwargs: object) -> object:
            events.append("assess")
            return next(assessments)

        def transition(
            _transaction_id: str,
            state: str,
            **_kwargs: object,
        ) -> bool:
            events.append(f"journal:{state}")
            return True

        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                side_effect=assess,
            ),
            mock.patch.object(
                apply_ruleset,
                "journal_transition",
                side_effect=transition,
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
            ) as delete,
        ):
            now = lambda: datetime(
                2026,
                7,
                30,
                0,
                2,
                tzinfo=timezone.utc,
            )
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            report["state"],
            "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE",
        )
        self.assertEqual(report["codes"], ["ROLLBACK_PRECONDITION_DRIFT"])
        self.assertEqual(
            events,
            [
                "journal:CREATED_ID_CONFIRMED",
                "assess",
                "assess",
                "journal:DELETE_INTENT_RECORDED",
                "assess",
                "journal:POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE",
            ],
        )
        delete.assert_not_called()

    def test_rollback_deadline_is_rechecked_immediately_before_delete(
        self,
    ) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        detail = apply_ruleset.RulesetDetail(
            ruleset_id=12,
            name="main",
            source_type="Repository",
            source=apply_ruleset.REPOSITORY,
            created_at="2099-01-01T00:00:00Z",
            updated_at="2099-01-01T00:00:01Z",
            current_user_can_bypass="never",
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            payload_matches=False,
            projection_state="mismatch",
            config_sha256="a" * 64,
        )
        mismatch = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=detail,
        )
        post_intent = datetime(
            2026,
            7,
            30,
            0,
            0,
            tzinfo=timezone.utc,
        )
        clock_values = iter(
            [
                post_intent,
                post_intent,
                post_intent,
                post_intent,
                post_intent,
                post_intent + timedelta(minutes=1),
                post_intent + timedelta(minutes=14, seconds=59),
                post_intent + timedelta(minutes=14, seconds=59),
                post_intent + timedelta(minutes=15, seconds=1),
            ]
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                side_effect=[mismatch, mismatch, mismatch],
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
            ) as delete,
        ):
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=lambda: next(clock_values),
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            report["codes"],
            ["ROLLBACK_WINDOW_EXPIRED_BEFORE_DELETE"],
        )
        delete.assert_not_called()

    def test_wall_clock_reversal_between_observations_blocks_delete(
        self,
    ) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        detail = apply_ruleset.RulesetDetail(
            ruleset_id=12,
            name="main",
            source_type="Repository",
            source=apply_ruleset.REPOSITORY,
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:01Z",
            current_user_can_bypass="never",
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            payload_matches=False,
            projection_state="mismatch",
            config_sha256="a" * 64,
        )
        mismatch = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=detail,
        )
        post_intent = datetime(
            2026,
            7,
            30,
            0,
            0,
            tzinfo=timezone.utc,
        )
        clock_values = iter(
            [
                post_intent,
                post_intent,
                post_intent,
                post_intent,
                post_intent,
                post_intent + timedelta(minutes=1),
                post_intent + timedelta(minutes=14),
                post_intent + timedelta(minutes=10),
            ]
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                side_effect=[mismatch, mismatch, mismatch],
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
            ) as delete,
        ):
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=lambda: next(clock_values),
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            report["codes"],
            ["ROLLBACK_WINDOW_EXPIRED_BEFORE_DELETE"],
        )
        delete.assert_not_called()

    def test_delete_intent_must_be_durable_before_delete(self) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        detail = apply_ruleset.RulesetDetail(
            ruleset_id=12,
            name="main",
            source_type="Repository",
            source=apply_ruleset.REPOSITORY,
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:01Z",
            current_user_can_bypass="never",
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            payload_matches=False,
        )
        mismatch = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=detail,
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                side_effect=[mismatch, mismatch, mismatch],
            ),
            mock.patch.object(
                apply_ruleset,
                "journal_transition",
                side_effect=[True, False],
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
            ) as delete,
        ):
            now = lambda: datetime(
                2026,
                7,
                30,
                0,
                2,
                tzinfo=timezone.utc,
            )
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 1)
        self.assertEqual(report["codes"], ["JOURNAL_UPDATE_FAILED"])
        delete.assert_not_called()

    def test_apply_guarded_ambiguous_delete_is_reconciled(self) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        mismatch = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("EFFECTIVE_RULE_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline, effective_rules=[{"x": 1}]),
            detail=apply_ruleset.RulesetDetail(
                ruleset_id=12,
                name="main",
                source_type="Repository",
                source=apply_ruleset.REPOSITORY,
                created_at="2026-07-30T00:00:00Z",
                updated_at="2026-07-30T00:00:01Z",
                current_user_can_bypass="must not bypass",
                projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
                payload_matches=False,
            ),
        )
        with mock.patch.object(apply_ruleset, "attempt_create", return_value=created), \
             mock.patch.object(apply_ruleset, "assess_postflight", side_effect=[mismatch, mismatch, mismatch]), \
             mock.patch.object(
                apply_ruleset,
                "attempt_delete",
                side_effect=apply_ruleset.MutationCommandUncertain("ambiguous delete"),
             ), \
             mock.patch.object(
                apply_ruleset,
                "verify_baseline_restored",
                return_value=True,
             ):
            now = lambda: datetime(2026, 7, 30, 0, 2, tzinfo=timezone.utc)
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 4)
        self.assertEqual(report["state"], "ROLLED_BACK_VERIFIED")
        self.assertEqual(
            report["codes"],
            ["DELETE_RESULT_RECONCILED_WITHOUT_RETRY"],
        )

    def test_apply_guarded_ambiguous_delete_never_retries_when_unresolved(
        self,
    ) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        detail = apply_ruleset.RulesetDetail(
            ruleset_id=12,
            name="main",
            source_type="Repository",
            source=apply_ruleset.REPOSITORY,
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:01Z",
            current_user_can_bypass="never",
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            payload_matches=False,
        )
        mismatch = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=detail,
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                side_effect=[mismatch, mismatch, mismatch],
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
                side_effect=apply_ruleset.MutationCommandUncertain(
                    "ambiguous"
                ),
            ) as delete,
            mock.patch.object(
                apply_ruleset,
                "verify_baseline_restored",
                return_value=False,
            ),
        ):
            now = lambda: datetime(
                2026,
                7,
                30,
                0,
                2,
                tzinfo=timezone.utc,
            )
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 3)
        self.assertEqual(report["state"], "STATE_UNKNOWN_AFTER_DELETE")
        self.assertEqual(delete.call_count, 1)

    def test_verified_rollback_is_not_success_when_terminal_journal_fails(
        self,
    ) -> None:
        created = apply_ruleset.CreatedReference(
            ruleset_id=12,
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            projection_state="exact",
        )
        detail = apply_ruleset.RulesetDetail(
            ruleset_id=12,
            name="main",
            source_type="Repository",
            source=apply_ruleset.REPOSITORY,
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:01Z",
            current_user_can_bypass="never",
            projected_payload=apply_ruleset.EXPECTED_PAYLOAD,
            payload_matches=False,
            projection_state="mismatch",
            config_sha256="a" * 64,
        )
        mismatch = apply_ruleset.PostflightAssessment(
            state="POSTFLIGHT_MISMATCH",
            codes=("DETAIL_SEMANTIC_MISMATCH",),
            snapshot=dict(self.baseline),
            detail=detail,
        )
        with (
            mock.patch.object(
                apply_ruleset,
                "attempt_create",
                return_value=created,
            ),
            mock.patch.object(
                apply_ruleset,
                "assess_postflight",
                side_effect=[mismatch, mismatch, mismatch],
            ),
            mock.patch.object(
                apply_ruleset,
                "attempt_delete",
                return_value=apply_ruleset.IncludedResponse(
                    status=204,
                    body="",
                ),
            ),
            mock.patch.object(
                apply_ruleset,
                "verify_baseline_restored",
                return_value=True,
            ),
            mock.patch.object(
                apply_ruleset,
                "journal_transition",
                side_effect=[True, True, False],
            ),
        ):
            now = lambda: datetime(
                2026,
                7,
                30,
                0,
                2,
                tzinfo=timezone.utc,
            )
            code, report = apply_ruleset.apply_guarded(
                self.reviewed_sha,
                self.frozen.sha256,
                approve_application=True,
                approve_conditional_rollback=True,
                now_function=now,
            )
        self.assertEqual(code, 3)
        self.assertEqual(report["state"], "JOURNAL_INCOMPLETE_AFTER_DELETE")
        self.assertEqual(report["mutation_state"], "confirmed_deleted")
        self.assertEqual(report["codes"], ["JOURNAL_UPDATE_FAILED"])

    def test_write_new_journal_never_overwrites_existing_record(self) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_text("original\n", encoding="utf-8")
        with mock.patch.object(
            apply_ruleset,
            "JOURNAL_PATH",
            self.journal,
        ):
            with self.assertRaises(apply_ruleset.RulesetGuardError):
                self.real_write_new_journal({"events": []})
        self.assertEqual(
            self.journal.read_text(encoding="utf-8"),
            "original\n",
        )


if __name__ == "__main__":
    unittest.main()
