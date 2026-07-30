#!/usr/bin/env python3
"""Apply the reviewed main ruleset through one fail-closed transaction."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parent
REPOSITORY = "ManabiGrid/manabigrid-site"
REMOTE_URL = "https://github.com/ManabiGrid/manabigrid-site.git"
MAIN_REF = "refs/heads/main"
API_VERSION = "2026-03-10"
PROPOSAL_PATH = ROOT / ".github" / "rulesets" / "main.proposal.json"
JOURNAL_PATH = ROOT / "review" / "ruleset-apply-transaction.json"
PUBLIC_BUILD_REPORT_URL = (
    "https://manabigrid.github.io/manabigrid-site/build-report.json"
)
REQUIRED_CHECK_NAME = "manabigrid-site-pr-gate"
REQUIRED_CHECK_INTEGRATION_ID = 15368
REQUIRED_PR_WORKFLOW = {
    "name": "Validate ManabiGrid site code",
    "path": ".github/workflows/pr-validate.yml",
    "state": "active",
}
REQUIRED_PAGES_WORKFLOW = {
    "name": "Build and deploy ManabiGrid Pages",
    "path": ".github/workflows/pages.yml",
    "state": "active",
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HTTP_STATUS_PATTERN = re.compile(
    r"^HTTP/(?:1\.[01]|2(?:\.0)?|3) ([0-9]{3})(?: .*)?$"
)
MAX_JSON_BYTES = 5_000_000
ROLLBACK_WINDOW = timedelta(minutes=15)
CHECK_FRESHNESS = timedelta(days=7)

EXPECTED_PAYLOAD: dict[str, Any] = {
    "name": "main",
    "target": "branch",
    "enforcement": "active",
    "bypass_actors": [],
    "conditions": {
        "ref_name": {
            "exclude": [],
            "include": ["~DEFAULT_BRANCH"],
        }
    },
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": True,
                "allowed_merge_methods": ["merge"],
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "do_not_enforce_on_create": False,
                "required_status_checks": [
                    {
                        "context": REQUIRED_CHECK_NAME,
                        "integration_id": REQUIRED_CHECK_INTEGRATION_ID,
                    }
                ],
            },
        },
    ],
}
EXPECTED_RULE_TYPES = tuple(rule["type"] for rule in EXPECTED_PAYLOAD["rules"])


class RulesetGuardError(RuntimeError):
    """A fail-closed guard failure with a stable, non-sensitive code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ObservationUnavailable(RulesetGuardError):
    """A read-only observation was unavailable or malformed."""


class MutationCommandUncertain(RuntimeError):
    """A mutation command started but its exact HTTP result is unavailable."""


@dataclass(frozen=True)
class FrozenPayload:
    raw_bytes: bytes
    text: str
    sha256: str
    decoded: dict[str, Any]


@dataclass(frozen=True)
class IncludedResponse:
    status: int
    body: str
    headers: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class CreatedReference:
    ruleset_id: int
    projected_payload: dict[str, Any] | None
    projection_state: str


@dataclass(frozen=True)
class RulesetDetail:
    ruleset_id: int
    name: str
    source_type: str
    source: str
    created_at: str
    updated_at: str
    current_user_can_bypass: str
    projected_payload: dict[str, Any] | None
    payload_matches: bool
    projection_state: str = "exact"
    config_sha256: str = ""

    def identity_marker(self) -> dict[str, Any]:
        return {
            "id": self.ruleset_id,
            "name": self.name,
            "source_type": self.source_type,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_user_can_bypass": self.current_user_can_bypass,
            "config_sha256": self.config_sha256,
        }


@dataclass(frozen=True)
class PostflightAssessment:
    state: str
    codes: tuple[str, ...]
    snapshot: dict[str, Any] | None
    detail: RulesetDetail | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_aware_utc(value: datetime, purpose: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            f"{purpose} did not provide a timezone-aware clock",
        )
    return value.astimezone(timezone.utc)


def rollback_window_is_open(
    post_intent_at: datetime,
    observed_at: datetime,
    post_intent_tick: float,
    observed_tick: float,
    *,
    previous_at: datetime | None = None,
    previous_tick: float | None = None,
) -> bool:
    intent = as_aware_utc(post_intent_at, "POST intent clock")
    observed = as_aware_utc(observed_at, "rollback clock")
    wall_elapsed = observed - intent
    monotonic_elapsed = observed_tick - post_intent_tick
    if (
        timedelta(0) > wall_elapsed
        or wall_elapsed > ROLLBACK_WINDOW
        or monotonic_elapsed < 0
        or monotonic_elapsed > ROLLBACK_WINDOW.total_seconds()
    ):
        return False
    if previous_at is not None:
        previous_wall = as_aware_utc(previous_at, "previous rollback clock")
        if observed < previous_wall:
            return False
    if previous_tick is not None and observed_tick < previous_tick:
        return False
    return True


def read_monotonic(
    monotonic_function: Callable[[], float],
    purpose: str,
) -> float:
    try:
        value = monotonic_function()
    except Exception as exc:
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            f"{purpose} was unavailable",
        ) from exc
    if type(value) not in {int, float} or not math.isfinite(value):
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            f"{purpose} was not a finite monotonic value",
        )
    return float(value)


def safe_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GH_HOST"] = "github.com"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GH_PROMPT_DISABLED"] = "1"
    environment["GH_NO_UPDATE_NOTIFIER"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["NO_COLOR"] = "1"
    for key in (
        "GH_REPO",
        "GH_DEBUG",
        "GH_FORCE_TTY",
        "GH_PAGER",
        "PAGER",
        "CLICOLOR",
        "CLICOLOR_FORCE",
    ):
        environment.pop(key, None)
    return environment


def run_command(
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        env=safe_environment(),
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    stdout = completed.stdout.decode("utf-8", errors="strict")
    stderr = completed.stderr.decode("utf-8", errors="strict")
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout,
        stderr,
    )


def require_success(
    completed: subprocess.CompletedProcess[str],
    purpose: str,
) -> str:
    if completed.returncode != 0:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_UNAVAILABLE",
            f"{purpose} failed with exit code {completed.returncode}",
        )
    if len(completed.stdout.encode("utf-8")) > MAX_JSON_BYTES:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} exceeded the response limit",
        )
    return completed.stdout


def reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def strict_json_loads(raw: str, purpose: str) -> object:
    if not isinstance(raw, str) or not raw.strip():
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} was empty",
        )
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} exceeded the response limit",
        )
    decoder = json.JSONDecoder(
        object_pairs_hook=reject_duplicate_json_keys,
        parse_constant=reject_nonstandard_json_constant,
    )
    position = 0
    while position < len(raw) and raw[position].isspace():
        position += 1
    try:
        value, end = decoder.raw_decode(raw, position)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} was malformed",
        ) from exc
    while end < len(raw) and raw[end].isspace():
        end += 1
    if end != len(raw):
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} contained trailing data",
        )
    return value


def validate_json_exact(
    actual: object,
    expected: object,
    path: str = "$",
) -> None:
    if type(actual) is not type(expected):
        raise RulesetGuardError(
            "BLOCKED_PAYLOAD_INVALID",
            (
                f"{path} expected {type(expected).__name__}, "
                f"got {type(actual).__name__}"
            ),
        )
    if isinstance(expected, dict):
        if set(actual) != set(expected):
            raise RulesetGuardError(
                "BLOCKED_PAYLOAD_INVALID",
                f"{path} object keys differ",
            )
        for key in expected:
            validate_json_exact(actual[key], expected[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        if len(actual) != len(expected):
            raise RulesetGuardError(
                "BLOCKED_PAYLOAD_INVALID",
                f"{path} array length differs",
            )
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
        raise RulesetGuardError(
            "BLOCKED_PAYLOAD_INVALID",
            f"{path} value differs",
        )


def validate_payload_contract(payload: object) -> dict[str, Any]:
    validate_json_exact(payload, EXPECTED_PAYLOAD)
    assert isinstance(payload, dict)
    return payload


def require_positive_id(value: object, purpose: str) -> int:
    if type(value) is not int or value <= 0:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} must be a positive integer",
        )
    return value


def freeze_payload(reviewed_sha256: str) -> FrozenPayload:
    if not SHA256_PATTERN.fullmatch(reviewed_sha256):
        raise RulesetGuardError(
            "BLOCKED_APPROVAL_SCOPE_MISMATCH",
            "reviewed payload SHA-256 must be 64 lowercase hexadecimal characters",
        )
    try:
        raw_bytes = PROPOSAL_PATH.read_bytes()
    except OSError as exc:
        raise RulesetGuardError(
            "BLOCKED_PAYLOAD_INVALID",
            "reviewed payload could not be read",
        ) from exc
    if len(raw_bytes) > MAX_JSON_BYTES:
        raise RulesetGuardError(
            "BLOCKED_PAYLOAD_INVALID",
            "reviewed payload exceeded the size limit",
        )
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256 != reviewed_sha256:
        raise RulesetGuardError(
            "BLOCKED_INPUT_DRIFT",
            "reviewed payload SHA-256 no longer matches the fixed file",
        )
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesetGuardError(
            "BLOCKED_PAYLOAD_INVALID",
            "reviewed payload was not valid UTF-8",
        ) from exc
    payload = strict_json_loads(text, "reviewed ruleset payload")
    validated = validate_payload_contract(payload)
    return FrozenPayload(
        raw_bytes=raw_bytes,
        text=text,
        sha256=actual_sha256,
        decoded=validated,
    )


def verify_frozen_payload_unchanged(payload: FrozenPayload) -> None:
    try:
        current = PROPOSAL_PATH.read_bytes()
    except OSError as exc:
        raise RulesetGuardError(
            "BLOCKED_DRIFT_BEFORE_POST",
            "payload became unreadable before POST",
        ) from exc
    if current != payload.raw_bytes:
        raise RulesetGuardError(
            "BLOCKED_DRIFT_BEFORE_POST",
            "payload changed after it was frozen",
        )


def flatten_slurped_pages(raw: str) -> list[dict[str, Any]]:
    decoded = strict_json_loads(raw, "paginated ruleset response")
    if not isinstance(decoded, list) or not decoded:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "paginated ruleset response must contain at least one page",
        )
    flattened: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for page in decoded:
        if not isinstance(page, list):
            raise RulesetGuardError(
                "BLOCKED_SNAPSHOT_MALFORMED",
                "every paginated ruleset page must be an array",
            )
        for item in page:
            if not isinstance(item, dict):
                raise RulesetGuardError(
                    "BLOCKED_SNAPSHOT_MALFORMED",
                    "ruleset list item was malformed",
                )
            ruleset_id = item.get("id")
            if (
                type(ruleset_id) is not int
                or ruleset_id <= 0
                or ruleset_id in seen_ids
            ):
                raise RulesetGuardError(
                    "BLOCKED_SNAPSHOT_MALFORMED",
                    "ruleset IDs must be unique positive integers",
                )
            seen_ids.add(ruleset_id)
            flattened.append(item)
    if not flattened and decoded != [[]]:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "an empty paginated ruleset response must be exactly one empty page",
        )
    if flattened and any(not page for page in decoded):
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "a non-empty paginated ruleset response contained an empty page",
        )
    return flattened


def parse_included_response(raw: str, purpose: str) -> IncludedResponse:
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} exceeded the response limit",
        )
    normalized = raw.replace("\r\n", "\n")
    if "\r" in normalized:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} had malformed line endings",
        )
    if "\n\n" not in normalized:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} had no complete HTTP header block",
        )
    header_block, body = normalized.split("\n\n", 1)
    header_lines = header_block.splitlines()
    if not header_lines:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} had no HTTP status",
        )
    match = HTTP_STATUS_PATTERN.fullmatch(header_lines[0])
    if match is None:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} had an invalid HTTP status",
        )
    if any(line.startswith("HTTP/") for line in header_lines[1:]):
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} had multiple HTTP status blocks",
        )
    header_values: dict[str, list[str]] = {}
    for line in header_lines[1:]:
        if not line or ":" not in line:
            raise RulesetGuardError(
                "BLOCKED_SNAPSHOT_MALFORMED",
                f"{purpose} had a malformed HTTP header",
            )
        name, value = line.split(":", 1)
        normalized_name = name.strip().casefold()
        normalized_value = value.strip()
        if not normalized_name or not normalized_value:
            raise RulesetGuardError(
                "BLOCKED_SNAPSHOT_MALFORMED",
                f"{purpose} had a malformed HTTP header",
            )
        header_values.setdefault(normalized_name, []).append(normalized_value)
    return IncludedResponse(
        status=int(match.group(1)),
        body=body,
        headers={
            name: tuple(values) for name, values in header_values.items()
        },
    )


def response_is_json(response: IncludedResponse) -> bool:
    values = response.headers.get("content-type", ())
    if len(values) != 1:
        return False
    media_type = values[0].split(";", 1)[0].strip().casefold()
    return media_type == "application/json"


def project_like(
    actual: object,
    template: object,
    path: str,
) -> object:
    if isinstance(template, dict):
        if not isinstance(actual, dict):
            raise RulesetGuardError(
                "POSTFLIGHT_UNVERIFIED",
                f"{path} was not an object",
            )
        allowed_extras: set[str] = set()
        if re.fullmatch(r"\$\.effective\.[^.]+", path):
            allowed_extras = {
                "ruleset_id",
                "ruleset_source_type",
                "ruleset_source",
            }
        if path in {
            "$.rules.pull_request.parameters",
            "$.effective.pull_request.parameters",
        }:
            optional_noop_defaults: dict[str, object] = {
                "dismissal_restriction": {
                    "enabled": False,
                    "allowed_actors": [],
                },
                "required_reviewers": [],
            }
            for key, no_op_value in optional_noop_defaults.items():
                if key not in actual:
                    continue
                try:
                    validate_json_exact(
                        actual[key],
                        no_op_value,
                        f"{path}.{key}",
                    )
                except RulesetGuardError as exc:
                    raise RulesetGuardError(
                        "POSTFLIGHT_MISMATCH",
                        f"{path}.{key} was not the exact no-op default",
                    ) from exc
                allowed_extras.add(key)
        unexpected = set(actual) - set(template) - allowed_extras
        if unexpected:
            raise RulesetGuardError(
                "POSTFLIGHT_MISMATCH",
                f"{path} contained unknown behavior fields",
            )
        projected: dict[str, object] = {}
        for key, expected_value in template.items():
            if key not in actual:
                raise RulesetGuardError(
                    "POSTFLIGHT_UNVERIFIED",
                    f"{path}.{key} was missing",
                )
            projected[key] = project_like(
                actual[key],
                expected_value,
                f"{path}.{key}",
            )
        return projected
    if isinstance(template, list):
        if not isinstance(actual, list) or len(actual) != len(template):
            raise RulesetGuardError(
                "POSTFLIGHT_MISMATCH",
                f"{path} array shape differed",
            )
        return [
            project_like(item, expected, f"{path}[{index}]")
            for index, (item, expected) in enumerate(zip(actual, template))
        ]
    if type(actual) is not type(template):
        raise RulesetGuardError(
            "POSTFLIGHT_MISMATCH",
            f"{path} type differed",
        )
    return actual


def project_response_payload(response: object) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise RulesetGuardError(
            "POSTFLIGHT_UNVERIFIED",
            "ruleset response was not an object",
        )
    known_server_metadata = {
        "id",
        "node_id",
        "source_type",
        "source",
        "created_at",
        "updated_at",
        "current_user_can_bypass",
        "_links",
    }
    unexpected_top_level = (
        set(response) - set(EXPECTED_PAYLOAD) - known_server_metadata
    )
    if unexpected_top_level:
        raise RulesetGuardError(
            "POSTFLIGHT_MISMATCH",
            "ruleset response contained unknown top-level fields",
        )
    expected_rules = {
        rule["type"]: rule for rule in EXPECTED_PAYLOAD["rules"]
    }
    actual_rules = response.get("rules")
    if not isinstance(actual_rules, list):
        raise RulesetGuardError(
            "POSTFLIGHT_UNVERIFIED",
            "ruleset response had no complete rules array",
        )
    indexed: dict[str, dict[str, Any]] = {}
    for rule in actual_rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            raise RulesetGuardError(
                "POSTFLIGHT_UNVERIFIED",
                "ruleset response contained a malformed rule",
            )
        rule_type = rule["type"]
        if rule_type in indexed:
            raise RulesetGuardError(
                "POSTFLIGHT_MISMATCH",
                "ruleset response contained a duplicate rule type",
            )
        indexed[rule_type] = rule
    if set(indexed) != set(expected_rules):
        raise RulesetGuardError(
            "POSTFLIGHT_MISMATCH",
            "ruleset response rule types differed",
        )
    projected = {
        "name": project_like(
            response.get("name"),
            EXPECTED_PAYLOAD["name"],
            "$.name",
        ),
        "target": project_like(
            response.get("target"),
            EXPECTED_PAYLOAD["target"],
            "$.target",
        ),
        "enforcement": project_like(
            response.get("enforcement"),
            EXPECTED_PAYLOAD["enforcement"],
            "$.enforcement",
        ),
        "bypass_actors": project_like(
            response.get("bypass_actors"),
            EXPECTED_PAYLOAD["bypass_actors"],
            "$.bypass_actors",
        ),
        "conditions": project_like(
            response.get("conditions"),
            EXPECTED_PAYLOAD["conditions"],
            "$.conditions",
        ),
        "rules": [
            project_like(
                indexed[rule_type],
                expected_rules[rule_type],
                f"$.rules.{rule_type}",
            )
            for rule_type in EXPECTED_RULE_TYPES
        ],
    }
    assert isinstance(projected, dict)
    return projected


def payload_projection_matches(projected: object) -> bool:
    try:
        validate_json_exact(projected, EXPECTED_PAYLOAD)
    except RulesetGuardError:
        return False
    return True


def parse_timestamp(value: object, purpose: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} timestamp was malformed",
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} timestamp was malformed",
        ) from exc
    if parsed.tzinfo is None:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} timestamp had no timezone",
        )
    return parsed.astimezone(timezone.utc)


def extract_created_reference(response: object) -> CreatedReference:
    if not isinstance(response, dict):
        raise RulesetGuardError(
            "POSTFLIGHT_UNVERIFIED",
            "created ruleset response was not an object",
        )
    ruleset_id = response.get("id")
    try:
        ruleset_id = require_positive_id(ruleset_id, "created ruleset ID")
    except RulesetGuardError as exc:
        raise RulesetGuardError(
            "POSTFLIGHT_UNVERIFIED",
            "created ruleset ID was not a positive integer",
        ) from exc
    try:
        projected = project_response_payload(response)
    except RulesetGuardError as exc:
        projected = None
        projection_state = (
            "mismatch"
            if exc.code == "POSTFLIGHT_MISMATCH"
            else "omitted"
        )
    else:
        projection_state = (
            "exact" if payload_projection_matches(projected) else "mismatch"
        )
    return CreatedReference(
        ruleset_id=ruleset_id,
        projected_payload=projected,
        projection_state=projection_state,
    )


def extract_ruleset_detail(response: object) -> RulesetDetail:
    if not isinstance(response, dict):
        raise RulesetGuardError(
            "POSTFLIGHT_UNVERIFIED",
            "ruleset detail response was not an object",
        )
    ruleset_id = response.get("id")
    try:
        ruleset_id = require_positive_id(ruleset_id, "ruleset detail ID")
    except RulesetGuardError as exc:
        raise RulesetGuardError(
            "POSTFLIGHT_UNVERIFIED",
            "ruleset detail ID was not a positive integer",
        ) from exc
    identity_values = {
        key: response.get(key)
        for key in (
            "name",
            "source_type",
            "source",
            "created_at",
            "updated_at",
            "current_user_can_bypass",
        )
    }
    if any(
        not isinstance(value, str) or not value
        for value in identity_values.values()
    ):
        raise RulesetGuardError(
            "POSTFLIGHT_UNVERIFIED",
            "ruleset detail identity was incomplete",
        )
    parse_timestamp(identity_values["created_at"], "ruleset detail")
    parse_timestamp(identity_values["updated_at"], "ruleset detail")
    non_config_metadata = {
        "id",
        "node_id",
        "source_type",
        "source",
        "created_at",
        "updated_at",
        "current_user_can_bypass",
        "_links",
    }
    config_marker = {
        key: value
        for key, value in response.items()
        if key not in non_config_metadata
    }
    try:
        projected = project_response_payload(response)
    except RulesetGuardError as exc:
        projected = None
        projection_state = (
            "mismatch"
            if exc.code == "POSTFLIGHT_MISMATCH"
            else "omitted"
        )
    else:
        projection_state = (
            "exact" if payload_projection_matches(projected) else "mismatch"
        )
    return RulesetDetail(
        ruleset_id=ruleset_id,
        name=identity_values["name"],
        source_type=identity_values["source_type"],
        source=identity_values["source"],
        created_at=identity_values["created_at"],
        updated_at=identity_values["updated_at"],
        current_user_can_bypass=identity_values[
            "current_user_can_bypass"
        ],
        projected_payload=projected,
        payload_matches=projection_state == "exact",
        projection_state=projection_state,
        config_sha256=canonical_digest(config_marker),
    )


def gh_base() -> list[str]:
    return [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {API_VERSION}",
        "-H",
        "Content-Type: application/json",
    ]


def api_get_json(endpoint: str, purpose: str) -> object:
    try:
        completed = run_command([*gh_base(), "--method", "GET", endpoint])
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_UNAVAILABLE",
            f"{purpose} could not be observed",
        ) from exc
    raw = require_success(completed, purpose)
    try:
        return strict_json_loads(raw, purpose)
    except RulesetGuardError as exc:
        raise ObservationUnavailable(exc.code, str(exc)) from exc


def api_get_ruleset_pages() -> list[dict[str, Any]]:
    try:
        completed = run_command(
            [
                *gh_base(),
                "--method",
                "GET",
                "--paginate",
                "--slurp",
                "-f",
                "includes_parents=true",
                "-f",
                "per_page=100",
                f"repos/{REPOSITORY}/rulesets",
            ]
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_UNAVAILABLE",
            "ruleset pages could not be observed",
        ) from exc
    raw = require_success(completed, "ruleset page verification")
    try:
        return flatten_slurped_pages(raw)
    except RulesetGuardError as exc:
        raise ObservationUnavailable(exc.code, str(exc)) from exc


def read_remote_main_sha() -> str:
    try:
        completed = run_command(
            ["git", "ls-remote", REMOTE_URL, MAIN_REF],
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_UNAVAILABLE",
            "official site main could not be resolved",
        ) from exc
    raw = require_success(completed, "official site main verification")
    fields = raw.split()
    if (
        len(fields) != 2
        or not SHA_PATTERN.fullmatch(fields[0])
        or fields[1] != MAIN_REF
    ):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "official site main did not resolve to one exact commit",
        )
    return fields[0]


def local_git_value(arguments: list[str], purpose: str) -> str:
    try:
        completed = run_command(["git", *arguments])
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise RulesetGuardError(
            "BLOCKED_INPUT_DRIFT",
            f"{purpose} could not be read",
        ) from exc
    if completed.returncode != 0:
        raise RulesetGuardError(
            "BLOCKED_INPUT_DRIFT",
            f"{purpose} failed with exit code {completed.returncode}",
        )
    return completed.stdout.strip()


def verify_local_release(reviewed_site_sha: str) -> None:
    if not SHA_PATTERN.fullmatch(reviewed_site_sha):
        raise RulesetGuardError(
            "BLOCKED_APPROVAL_SCOPE_MISMATCH",
            "reviewed site SHA must be one lowercase 40-character commit",
        )
    if local_git_value(["branch", "--show-current"], "local branch") != "main":
        raise RulesetGuardError(
            "BLOCKED_INPUT_DRIFT",
            "local checkout is not on main",
        )
    if local_git_value(["rev-parse", "HEAD"], "local HEAD") != reviewed_site_sha:
        raise RulesetGuardError(
            "BLOCKED_INPUT_DRIFT",
            "local HEAD differs from the reviewed site SHA",
        )
    if (
        local_git_value(["remote", "get-url", "origin"], "origin URL")
        != REMOTE_URL
    ):
        raise RulesetGuardError(
            "BLOCKED_INPUT_DRIFT",
            "origin is not the fixed site repository",
        )
    status = local_git_value(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        "worktree status",
    )
    if status:
        raise RulesetGuardError(
            "BLOCKED_DIRTY_WORKTREE",
            "site worktree is not clean",
        )
    if read_remote_main_sha() != reviewed_site_sha:
        raise RulesetGuardError(
            "BLOCKED_INPUT_DRIFT",
            "official site main differs from the reviewed site SHA",
        )


def run_local_contract_checkers() -> None:
    for script in ("check_workflow.py", "check_pr_workflow.py"):
        try:
            completed = run_command([sys.executable, script], timeout=120)
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise RulesetGuardError(
                "BLOCKED_CHECK_OR_INTEGRATION_DRIFT",
                f"{script} could not run",
            ) from exc
        if completed.returncode != 0:
            raise RulesetGuardError(
                "BLOCKED_CHECK_OR_INTEGRATION_DRIFT",
                f"{script} failed with exit code {completed.returncode}",
            )


def normalize_ruleset_summaries(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in items:
        marker: dict[str, Any] = {}
        for key in (
            "id",
            "name",
            "target",
            "source_type",
            "source",
            "enforcement",
            "created_at",
            "updated_at",
        ):
            if key not in item:
                raise ObservationUnavailable(
                    "BLOCKED_SNAPSHOT_MALFORMED",
                    "ruleset summary was incomplete",
                )
            marker[key] = item[key]
        if type(marker["id"]) is not int or marker["id"] <= 0:
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_MALFORMED",
                "ruleset summary ID was malformed",
            )
        if any(
            not isinstance(marker[key], str) or not marker[key]
            for key in marker
            if key != "id"
        ):
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_MALFORMED",
                "ruleset summary identity was malformed",
            )
        parse_timestamp(marker["created_at"], "ruleset summary")
        parse_timestamp(marker["updated_at"], "ruleset summary")
        summaries.append(marker)
    return sorted(summaries, key=lambda item: item["id"])


def normalize_effective_rules(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "main effective rules response was not an array",
        )
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for rule in payload:
        if not isinstance(rule, dict):
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_MALFORMED",
                "main effective rule was malformed",
            )
        ruleset_id = rule.get("ruleset_id")
        rule_type = rule.get("type")
        source_type = rule.get("ruleset_source_type")
        source = rule.get("ruleset_source")
        if (
            type(ruleset_id) is not int
            or ruleset_id <= 0
            or not isinstance(rule_type, str)
            or not rule_type
            or not isinstance(source_type, str)
            or not source_type
            or not isinstance(source, str)
            or not source
        ):
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_MALFORMED",
                "main effective rule identity was malformed",
            )
        key = (ruleset_id, rule_type)
        if key in seen:
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_MALFORMED",
                "main effective rules contained a duplicate",
            )
        seen.add(key)
        normalized.append(rule)
    return sorted(
        normalized,
        key=lambda item: (item["ruleset_id"], item["type"]),
    )


def marker_from_object(
    payload: object,
    expected: dict[str, Any],
    purpose: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            f"{purpose} was not an object",
        )
    marker: dict[str, Any] = {}
    for key, expected_value in expected.items():
        if key not in payload:
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_MALFORMED",
                f"{purpose} omitted {key}",
            )
        actual = payload[key]
        if type(actual) is not type(expected_value):
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_MALFORMED",
                f"{purpose} had an invalid {key}",
            )
        if expected_value is not None and actual != expected_value:
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_CONFLICT",
                f"{purpose} did not match the reviewed setting",
            )
        marker[key] = actual
    return marker


def load_release_gate_marker(
    site_sha: str,
    now: datetime,
) -> dict[str, Any]:
    commit = api_get_json(
        f"repos/{REPOSITORY}/commits/{site_sha}",
        "site merge commit verification",
    )
    if not isinstance(commit, dict) or commit.get("sha") != site_sha:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "site merge commit response was malformed",
        )
    parents = commit.get("parents")
    if not isinstance(parents, list) or len(parents) != 2:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "site main is not one exact two-parent merge",
        )
    head_sha = (
        parents[1].get("sha") if isinstance(parents[1], dict) else None
    )
    if not isinstance(head_sha, str) or not SHA_PATTERN.fullmatch(head_sha):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "reviewed PR head SHA was malformed",
        )
    pulls = api_get_json(
        f"repos/{REPOSITORY}/commits/{site_sha}/pulls?per_page=100",
        "associated pull request verification",
    )
    if not isinstance(pulls, list):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "associated pull request response was malformed",
        )
    matches: list[dict[str, Any]] = []
    for pull_request in pulls:
        if not isinstance(pull_request, dict):
            raise ObservationUnavailable(
                "BLOCKED_SNAPSHOT_MALFORMED",
                "associated pull request item was malformed",
            )
        base = pull_request.get("base")
        head = pull_request.get("head")
        if (
            pull_request.get("state") == "closed"
            and isinstance(pull_request.get("merged_at"), str)
            and isinstance(base, dict)
            and base.get("ref") == "main"
            and isinstance(head, dict)
            and head.get("sha") == head_sha
        ):
            matches.append(pull_request)
    if len(matches) != 1:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "site main was not bound to exactly one reviewed PR",
        )
    number = matches[0].get("number")
    if type(number) is not int or number <= 0:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "reviewed PR number was malformed",
        )
    checks = api_get_json(
        f"repos/{REPOSITORY}/commits/{head_sha}/check-runs?per_page=100",
        "required PR gate verification",
    )
    if not isinstance(checks, dict):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "required PR gate response was malformed",
        )
    total_count = checks.get("total_count")
    check_runs = checks.get("check_runs")
    if (
        type(total_count) is not int
        or not isinstance(check_runs, list)
        or total_count != len(check_runs)
    ):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "required PR gate result set was incomplete",
        )
    required = [
        check
        for check in check_runs
        if isinstance(check, dict)
        and check.get("name") == REQUIRED_CHECK_NAME
    ]
    if len(required) != 1:
        raise ObservationUnavailable(
            "BLOCKED_CHECK_OR_INTEGRATION_DRIFT",
            "required PR gate was missing or duplicated",
        )
    check = required[0]
    app = check.get("app")
    completed_at = parse_timestamp(
        check.get("completed_at"),
        "required PR gate",
    )
    age = now.astimezone(timezone.utc) - completed_at
    if (
        check.get("head_sha") != head_sha
        or check.get("status") != "completed"
        or check.get("conclusion") != "success"
        or not isinstance(app, dict)
        or app.get("slug") != "github-actions"
        or type(app.get("id")) is not int
        or app.get("id") != REQUIRED_CHECK_INTEGRATION_ID
        or age < timedelta(minutes=-5)
        or age > CHECK_FRESHNESS
    ):
        raise ObservationUnavailable(
            "BLOCKED_CHECK_OR_INTEGRATION_DRIFT",
            "required PR gate was stale or did not match the reviewed integration",
        )
    check_id = check.get("id")
    if type(check_id) is not int or check_id <= 0:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "required PR gate ID was malformed",
        )
    return {
        "pull_request": number,
        "head_sha": head_sha,
        "check_id": check_id,
        "completed_at": check.get("completed_at"),
        "integration_id": app.get("id"),
    }


def load_latest_daily_marker(site_sha: str) -> dict[str, Any]:
    runs = api_get_json(
        (
            f"repos/{REPOSITORY}/actions/workflows/pages.yml/runs"
            "?event=schedule&branch=main&per_page=100"
        ),
        "daily workflow verification",
    )
    if not isinstance(runs, dict):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "daily workflow response was malformed",
        )
    total_count = runs.get("total_count")
    workflow_runs = runs.get("workflow_runs")
    if (
        type(total_count) is not int
        or not isinstance(workflow_runs, list)
        or total_count < len(workflow_runs)
        or len(workflow_runs) > 100
    ):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "daily workflow result set was incomplete",
        )
    matches = [
        run
        for run in workflow_runs
        if isinstance(run, dict) and run.get("head_sha") == site_sha
    ]
    if not matches:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "no daily workflow run exists for the reviewed site SHA",
        )
    matches.sort(key=lambda item: str(item.get("created_at")), reverse=True)
    latest = matches[0]
    run_id = latest.get("id")
    if (
        type(run_id) is not int
        or run_id <= 0
        or latest.get("event") != "schedule"
        or latest.get("status") != "completed"
        or latest.get("conclusion") != "success"
        or not isinstance(latest.get("created_at"), str)
    ):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "latest daily workflow for the reviewed site SHA is not successful",
        )
    parse_timestamp(latest["created_at"], "daily workflow")
    return {
        "id": run_id,
        "head_sha": site_sha,
        "event": "schedule",
        "status": "completed",
        "conclusion": "success",
        "created_at": latest["created_at"],
    }


def load_public_build_marker() -> dict[str, Any]:
    request = Request(
        PUBLIC_BUILD_REPORT_URL,
        headers={"User-Agent": "ManabiGrid-Ruleset-Guard/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            final_url = response.geturl()
            body = response.read(MAX_JSON_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_UNAVAILABLE",
            "public build report could not be observed",
        ) from exc
    if len(body) > MAX_JSON_BYTES:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "public build report exceeded the response limit",
        )
    if final_url != PUBLIC_BUILD_REPORT_URL:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "public build report redirected outside its fixed URL",
        )
    try:
        raw = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "public build report was not UTF-8",
        ) from exc
    try:
        payload = strict_json_loads(raw, "public build report")
    except RulesetGuardError as exc:
        raise ObservationUnavailable(exc.code, str(exc)) from exc
    if not isinstance(payload, dict):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "public build report was not an object",
        )
    publication = payload.get("publication")
    source = payload.get("source")
    if not isinstance(publication, dict) or not isinstance(source, dict):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "public build report provenance was incomplete",
        )
    site_sha = publication.get("site_commit")
    source_sha = source.get("commit")
    if (
        not isinstance(site_sha, str)
        or not SHA_PATTERN.fullmatch(site_sha)
        or not isinstance(source_sha, str)
        or not SHA_PATTERN.fullmatch(source_sha)
        or publication.get("site_repository") != REPOSITORY
        or source.get("repository")
        != "https://github.com/ManabiGrid/manabigrid"
    ):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "public build report provenance was malformed",
        )
    return {
        "site_sha": site_sha,
        "source_sha": source_sha,
    }


def load_operational_markers(
    site_sha: str,
    now: datetime,
) -> dict[str, Any]:
    versions = api_get_json("versions", "GitHub API version verification")
    if (
        not isinstance(versions, list)
        or not versions
        or any(not isinstance(version, str) for version in versions)
        or API_VERSION not in versions
    ):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "reviewed GitHub API version is not currently offered",
        )
    pr_workflow = api_get_json(
        f"repos/{REPOSITORY}/actions/workflows/pr-validate.yml",
        "PR workflow verification",
    )
    pages_workflow = api_get_json(
        f"repos/{REPOSITORY}/actions/workflows/pages.yml",
        "Pages workflow verification",
    )
    permissions = api_get_json(
        f"repos/{REPOSITORY}/actions/permissions/workflow",
        "Actions permission verification",
    )
    environment = api_get_json(
        f"repos/{REPOSITORY}/environments/github-pages",
        "Pages environment verification",
    )
    branch_policies = api_get_json(
        (
            f"repos/{REPOSITORY}/environments/github-pages/"
            "deployment-branch-policies"
        ),
        "Pages branch policy verification",
    )
    if not isinstance(environment, dict):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "Pages environment response was malformed",
        )
    deployment_policy = environment.get("deployment_branch_policy")
    if not isinstance(deployment_policy, dict):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "Pages deployment policy was malformed",
        )
    pages_policy = marker_from_object(
        deployment_policy,
        {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
        "Pages deployment policy",
    )
    if not isinstance(branch_policies, dict):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_MALFORMED",
            "Pages branch policy response was malformed",
        )
    if (
        branch_policies.get("total_count") != 1
        or not isinstance(branch_policies.get("branch_policies"), list)
        or len(branch_policies["branch_policies"]) != 1
        or not isinstance(branch_policies["branch_policies"][0], dict)
    ):
        raise ObservationUnavailable(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "Pages is not restricted to one branch policy",
        )
    page_branch = marker_from_object(
        branch_policies["branch_policies"][0],
        {"name": "main", "type": "branch"},
        "Pages branch policy",
    )
    actions_permissions = marker_from_object(
        permissions,
        {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        "Actions permission",
    )
    return {
        "api_version": API_VERSION,
        "pr_workflow": marker_from_object(
            pr_workflow,
            REQUIRED_PR_WORKFLOW,
            "PR workflow",
        ),
        "pages_workflow": marker_from_object(
            pages_workflow,
            REQUIRED_PAGES_WORKFLOW,
            "Pages workflow",
        ),
        "actions_permissions": actions_permissions,
        "pages_environment": {
            "name": environment.get("name"),
            "deployment_branch_policy": pages_policy,
            "branch_policy": page_branch,
        },
        "release_gate": load_release_gate_marker(site_sha, now),
        "daily_workflow": load_latest_daily_marker(site_sha),
        "public_build": load_public_build_marker(),
    }


def capture_remote_snapshot(
    site_sha: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = now or utc_now()
    remote_main = read_remote_main_sha()
    repository = api_get_json(
        f"repos/{REPOSITORY}",
        "repository verification",
    )
    repository_marker = marker_from_object(
        repository,
        {
            "full_name": REPOSITORY,
            "default_branch": "main",
        },
        "repository",
    )
    rulesets = normalize_ruleset_summaries(api_get_ruleset_pages())
    effective = normalize_effective_rules(
        api_get_json(
            f"repos/{REPOSITORY}/rules/branches/main",
            "main effective rules verification",
        )
    )
    return {
        "site_sha": remote_main,
        "repository": repository_marker,
        "rulesets": rulesets,
        "effective_rules": effective,
        "operational": load_operational_markers(site_sha, observed_now),
    }


def validate_preflight_snapshot(
    snapshot: dict[str, Any],
    reviewed_site_sha: str,
) -> None:
    if snapshot["site_sha"] != reviewed_site_sha:
        raise RulesetGuardError(
            "BLOCKED_INPUT_DRIFT",
            "remote main changed during preflight",
        )
    if snapshot["rulesets"]:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "ruleset baseline is not the reviewed zero-ruleset state",
        )
    if snapshot["effective_rules"]:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "main already has effective rules",
        )
    public_build = snapshot["operational"]["public_build"]
    if public_build["site_sha"] != reviewed_site_sha:
        raise RulesetGuardError(
            "BLOCKED_SNAPSHOT_CONFLICT",
            "public Pages is not at the reviewed site SHA",
        )


def perform_preflight(
    reviewed_site_sha: str,
    payload: FrozenPayload,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    verify_local_release(reviewed_site_sha)
    verify_frozen_payload_unchanged(payload)
    run_local_contract_checkers()
    snapshot = capture_remote_snapshot(reviewed_site_sha, now=now)
    validate_preflight_snapshot(snapshot, reviewed_site_sha)
    return snapshot


def recheck_before_post(
    reviewed_site_sha: str,
    payload: FrozenPayload,
    baseline: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    verify_local_release(reviewed_site_sha)
    verify_frozen_payload_unchanged(payload)
    current = capture_remote_snapshot(reviewed_site_sha, now=now)
    if current != baseline:
        raise RulesetGuardError(
            "BLOCKED_DRIFT_BEFORE_POST",
            "remote state changed after the reviewed snapshot",
        )


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_new_journal(record: dict[str, Any]) -> None:
    try:
        parent_existed = JOURNAL_PATH.parent.exists()
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            fsync_directory(JOURNAL_PATH.parent.parent)
        payload = (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            JOURNAL_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            "a previous ruleset transaction journal already exists",
        ) from exc
    except OSError as exc:
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            "ruleset transaction journal could not be created",
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(JOURNAL_PATH.parent)
    except OSError as exc:
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            "ruleset transaction journal could not be written",
        ) from exc


def update_journal(
    transaction_id: str,
    state: str,
    *,
    ruleset_id: int | None = None,
    codes: tuple[str, ...] = (),
    now: datetime | None = None,
) -> None:
    try:
        raw = JOURNAL_PATH.read_text(encoding="utf-8")
        journal = strict_json_loads(raw, "ruleset transaction journal")
    except (OSError, UnicodeError, RulesetGuardError) as exc:
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            "ruleset transaction journal could not be verified",
        ) from exc
    if (
        not isinstance(journal, dict)
        or journal.get("transaction_id") != transaction_id
        or not isinstance(journal.get("events"), list)
    ):
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            "ruleset transaction journal identity changed",
        )
    event: dict[str, Any] = {
        "state": state,
        "at": (now or utc_now()).isoformat(),
        "codes": list(codes),
    }
    if ruleset_id is not None:
        event["ruleset_id"] = ruleset_id
    journal["events"].append(event)
    encoded = (
        json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=JOURNAL_PATH.parent,
        prefix=".ruleset-journal.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, JOURNAL_PATH)
    fsync_directory(JOURNAL_PATH.parent)


def fsync_directory(directory: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError as exc:
        raise RulesetGuardError(
            "BLOCKED_AUTHORIZATION",
            "transaction journal directory could not be synchronized",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def journal_transition(
    transaction_id: str,
    state: str,
    *,
    ruleset_id: int | None = None,
    codes: tuple[str, ...] = (),
    now: datetime | None = None,
) -> bool:
    try:
        update_journal(
            transaction_id,
            state,
            ruleset_id=ruleset_id,
            codes=codes,
            now=now,
        )
    except (RulesetGuardError, OSError, UnicodeError):
        return False
    return True


def new_journal_record(
    transaction_id: str,
    reviewed_site_sha: str,
    payload: FrozenPayload,
    baseline: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "repository": REPOSITORY,
        "reviewed_site_sha": reviewed_site_sha,
        "reviewed_payload_sha256": payload.sha256,
        "baseline_sha256": canonical_digest(baseline),
        "events": [
            {
                "state": "POST_INTENT_RECORDED",
                "at": now.isoformat(),
                "codes": [],
            }
        ],
    }


def attempt_create(payload: FrozenPayload) -> CreatedReference:
    command = [
        *gh_base(),
        "--include",
        "--method",
        "POST",
        f"repos/{REPOSITORY}/rulesets",
        "--input",
        "-",
    ]
    try:
        completed = run_command(
            command,
            input_bytes=payload.raw_bytes,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise MutationCommandUncertain(
            "POST command result was unavailable"
        ) from exc
    try:
        response = parse_included_response(
            completed.stdout,
            "ruleset POST response",
        )
    except RulesetGuardError as exc:
        raise MutationCommandUncertain(
            "POST response could not be parsed uniquely"
        ) from exc
    if (
        completed.returncode != 0
        or response.status != 201
        or not response_is_json(response)
    ):
        raise MutationCommandUncertain(
            "POST did not return one exact HTTP 201 response"
        )
    try:
        decoded = strict_json_loads(response.body, "ruleset POST body")
        return extract_created_reference(decoded)
    except RulesetGuardError as exc:
        raise MutationCommandUncertain(
            "POST body did not identify one exact created ruleset"
        ) from exc


def load_ruleset_detail(ruleset_id: int) -> RulesetDetail:
    require_positive_id(ruleset_id, "ruleset detail ID")
    payload = api_get_json(
        f"repos/{REPOSITORY}/rulesets/{ruleset_id}",
        "created ruleset detail verification",
    )
    return extract_ruleset_detail(payload)


def expected_effective_projection(
    effective_rules: list[dict[str, Any]],
    ruleset_id: int,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    require_positive_id(ruleset_id, "effective ruleset ID")
    codes: list[str] = []
    indexed: dict[str, dict[str, Any]] = {}
    for rule in effective_rules:
        if rule.get("ruleset_id") != ruleset_id:
            return None, ("CONCURRENT_EFFECTIVE_RULESET",)
        rule_type = rule.get("type")
        if rule_type in indexed:
            return None, ("DUPLICATE_EFFECTIVE_RULE",)
        indexed[rule_type] = rule
    if set(indexed) != set(EXPECTED_RULE_TYPES):
        return None, ("EFFECTIVE_RULE_SET_INCOMPLETE",)
    expected_rules = {
        rule["type"]: rule for rule in EXPECTED_PAYLOAD["rules"]
    }
    projected_rules: list[object] = []
    for rule_type in EXPECTED_RULE_TYPES:
        rule = indexed[rule_type]
        if (
            rule.get("ruleset_source_type") != "Repository"
            or rule.get("ruleset_source") != REPOSITORY
        ):
            return None, ("EFFECTIVE_RULE_SOURCE_MISMATCH",)
        try:
            projected = project_like(
                rule,
                expected_rules[rule_type],
                f"$.effective.{rule_type}",
            )
        except RulesetGuardError as exc:
            if exc.code == "POSTFLIGHT_MISMATCH":
                return None, ("EFFECTIVE_RULE_SEMANTIC_MISMATCH",)
            return None, ("EFFECTIVE_RULE_MALFORMED",)
        projected_rules.append(projected)
    candidate = {
        **EXPECTED_PAYLOAD,
        "rules": projected_rules,
    }
    if not payload_projection_matches(candidate):
        codes.append("EFFECTIVE_RULE_SEMANTIC_MISMATCH")
    return candidate, tuple(codes)


def compare_detail_identity(
    actual: RulesetDetail,
    expected: RulesetDetail,
) -> bool:
    return actual.identity_marker() == expected.identity_marker()


def summary_matches_detail(
    summary: dict[str, Any],
    detail: RulesetDetail,
) -> bool:
    return all(
        summary.get(key) == value
        for key, value in {
            "id": detail.ruleset_id,
            "name": detail.name,
            "target": "branch",
            "source_type": detail.source_type,
            "source": detail.source,
            "enforcement": "active",
            "created_at": detail.created_at,
            "updated_at": detail.updated_at,
        }.items()
    )


def assess_postflight(
    baseline: dict[str, Any],
    created: CreatedReference,
    reviewed_site_sha: str,
    *,
    now: datetime | None = None,
) -> PostflightAssessment:
    try:
        snapshot = capture_remote_snapshot(reviewed_site_sha, now=now)
        detail = load_ruleset_detail(created.ruleset_id)
    except (RulesetGuardError, ObservationUnavailable):
        return PostflightAssessment(
            state="POSTFLIGHT_UNVERIFIED",
            codes=("OBSERVATION_UNAVAILABLE",),
            snapshot=None,
            detail=None,
        )
    if (
        snapshot["site_sha"] != baseline["site_sha"]
        or snapshot["repository"] != baseline["repository"]
        or snapshot["operational"] != baseline["operational"]
    ):
        return PostflightAssessment(
            state="POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE",
            codes=("CONCURRENT_OPERATIONAL_DRIFT",),
            snapshot=snapshot,
            detail=detail,
        )
    summaries = snapshot["rulesets"]
    if len(summaries) != 1 or summaries[0].get("id") != created.ruleset_id:
        return PostflightAssessment(
            state="POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE",
            codes=("CONCURRENT_RULESET_DRIFT",),
            snapshot=snapshot,
            detail=detail,
        )
    if not summary_matches_detail(summaries[0], detail):
        return PostflightAssessment(
            state="POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE",
            codes=("RULESET_IDENTITY_DRIFT",),
            snapshot=snapshot,
            detail=detail,
        )
    _effective_projection, effective_codes = expected_effective_projection(
        snapshot["effective_rules"],
        created.ruleset_id,
    )
    if effective_codes == ("EFFECTIVE_RULE_SET_INCOMPLETE",):
        return PostflightAssessment(
            state="POSTFLIGHT_UNVERIFIED",
            codes=effective_codes,
            snapshot=snapshot,
            detail=detail,
        )
    if effective_codes and effective_codes != (
        "EFFECTIVE_RULE_SEMANTIC_MISMATCH",
    ):
        return PostflightAssessment(
            state="POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE",
            codes=effective_codes,
            snapshot=snapshot,
            detail=detail,
        )
    detail_matches = detail.projection_state == "exact"
    bypass_matches = detail.current_user_can_bypass == "never"
    if (
        created.projection_state in {"exact", "omitted"}
        and detail_matches
        and bypass_matches
        and not effective_codes
        and detail.name == "main"
        and detail.source_type == "Repository"
        and detail.source == REPOSITORY
    ):
        return PostflightAssessment(
            state="APPLIED_CONFIG_VERIFIED_BEHAVIOR_PENDING",
            codes=(),
            snapshot=snapshot,
            detail=detail,
        )
    if (
        created.projection_state == "mismatch"
        or detail.projection_state == "omitted"
    ):
        return PostflightAssessment(
            state="POSTFLIGHT_UNVERIFIED",
            codes=("CREATE_OR_DETAIL_RESPONSE_INCONSISTENT",),
            snapshot=snapshot,
            detail=detail,
        )
    mismatch_codes: list[str] = []
    if not detail_matches:
        mismatch_codes.append("DETAIL_SEMANTIC_MISMATCH")
    if not bypass_matches:
        mismatch_codes.append("DETAIL_BYPASS_MISMATCH")
    mismatch_codes.extend(effective_codes)
    return PostflightAssessment(
        state="POSTFLIGHT_MISMATCH",
        codes=tuple(sorted(mismatch_codes)),
        snapshot=snapshot,
        detail=detail,
    )


def attempt_delete(ruleset_id: int) -> IncludedResponse:
    require_positive_id(ruleset_id, "DELETE ruleset ID")
    command = [
        *gh_base(),
        "--include",
        "--method",
        "DELETE",
        f"repos/{REPOSITORY}/rulesets/{ruleset_id}",
    ]
    try:
        completed = run_command(command, timeout=30)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise MutationCommandUncertain(
            "DELETE command result was unavailable"
        ) from exc
    try:
        response = parse_included_response(
            completed.stdout,
            "ruleset DELETE response",
        )
    except RulesetGuardError as exc:
        raise MutationCommandUncertain(
            "DELETE response could not be parsed uniquely"
        ) from exc
    if (
        completed.returncode != 0
        or response.status != 204
        or response.body != ""
    ):
        raise MutationCommandUncertain(
            "DELETE did not return one exact empty HTTP 204 response"
        )
    return response


def detail_is_exact_404(ruleset_id: int) -> bool:
    try:
        require_positive_id(ruleset_id, "deleted ruleset ID")
    except RulesetGuardError:
        return False
    try:
        completed = run_command(
            [
                *gh_base(),
                "--include",
                "--method",
                "GET",
                f"repos/{REPOSITORY}/rulesets/{ruleset_id}",
            ]
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False
    try:
        response = parse_included_response(
            completed.stdout,
            "deleted ruleset detail response",
        )
    except RulesetGuardError:
        return False
    if (
        response.status != 404
        or completed.returncode == 0
        or not response_is_json(response)
    ):
        return False
    try:
        body = strict_json_loads(
            response.body,
            "deleted ruleset detail body",
        )
    except RulesetGuardError:
        return False
    return (
        isinstance(body, dict)
        and body.get("status") == "404"
        and body.get("message") == "Not Found"
    )


def verify_baseline_restored(
    baseline: dict[str, Any],
    reviewed_site_sha: str,
    ruleset_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    if not detail_is_exact_404(ruleset_id):
        return False
    try:
        current = capture_remote_snapshot(reviewed_site_sha, now=now)
    except (RulesetGuardError, ObservationUnavailable):
        return False
    return current == baseline


def result_record(
    state: str,
    *,
    mutation_state: str,
    retry_allowed: bool,
    rollback_allowed: bool,
    ruleset_id: int | None = None,
    codes: tuple[str, ...] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": state,
        "mutation_state": mutation_state,
        "retry_allowed": retry_allowed,
        "rollback_allowed": rollback_allowed,
        "codes": list(codes),
    }
    if ruleset_id is not None:
        result["ruleset_id"] = ruleset_id
    return result


def apply_guarded(
    reviewed_site_sha: str,
    reviewed_payload_sha256: str,
    *,
    approve_application: bool,
    approve_conditional_rollback: bool,
    now_function: Callable[[], datetime] = utc_now,
    monotonic_function: Callable[[], float] = time.monotonic,
) -> tuple[int, dict[str, Any]]:
    if not approve_application or not approve_conditional_rollback:
        return (
            2,
            result_record(
                "BLOCKED_AUTHORIZATION",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
                codes=("BOTH_EXPLICIT_APPROVAL_FLAGS_REQUIRED",),
            ),
        )
    if JOURNAL_PATH.exists():
        return (
            2,
            result_record(
                "BLOCKED_AUTHORIZATION",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
                codes=("EXISTING_TRANSACTION_JOURNAL",),
            ),
        )
    try:
        payload = freeze_payload(reviewed_payload_sha256)
        started_at = now_function()
        baseline = perform_preflight(
            reviewed_site_sha,
            payload,
            now=started_at,
        )
        recheck_before_post(
            reviewed_site_sha,
            payload,
            baseline,
            now=now_function(),
        )
    except RulesetGuardError as exc:
        return (
            1,
            result_record(
                exc.code,
                mutation_state="none",
                retry_allowed=True,
                rollback_allowed=False,
            ),
        )
    except Exception:
        return (
            1,
            result_record(
                "BLOCKED_INTERNAL_ERROR",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
            ),
        )
    transaction_id = uuid.uuid4().hex
    try:
        post_intent_at = as_aware_utc(
            now_function(),
            "POST intent clock",
        )
        post_intent_tick = read_monotonic(
            monotonic_function,
            "POST intent monotonic clock",
        )
        write_new_journal(
            new_journal_record(
                transaction_id,
                reviewed_site_sha,
                payload,
                baseline,
                now=post_intent_at,
            )
        )
    except RulesetGuardError as exc:
        return (
            2,
            result_record(
                exc.code,
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
            ),
        )
    try:
        pre_post_at = as_aware_utc(
            now_function(),
            "pre-POST clock",
        )
        pre_post_tick = read_monotonic(
            monotonic_function,
            "pre-POST monotonic clock",
        )
    except RulesetGuardError:
        journal_transition(
            transaction_id,
            "BLOCKED_DRIFT_BEFORE_POST",
            codes=("POST_CLOCK_INVALID",),
        )
        return (
            1,
            result_record(
                "BLOCKED_DRIFT_BEFORE_POST",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
                codes=("POST_CLOCK_INVALID",),
            ),
        )
    if not rollback_window_is_open(
        post_intent_at,
        pre_post_at,
        post_intent_tick,
        pre_post_tick,
        previous_at=post_intent_at,
        previous_tick=post_intent_tick,
    ):
        journal_transition(
            transaction_id,
            "BLOCKED_DRIFT_BEFORE_POST",
            codes=("POST_WINDOW_EXPIRED_OR_CLOCK_REVERSED",),
            now=pre_post_at,
        )
        return (
            1,
            result_record(
                "BLOCKED_DRIFT_BEFORE_POST",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
                codes=("POST_WINDOW_EXPIRED_OR_CLOCK_REVERSED",),
            ),
        )
    try:
        recheck_before_post(
            reviewed_site_sha,
            payload,
            baseline,
            now=pre_post_at,
        )
    except RulesetGuardError as exc:
        journal_transition(
            transaction_id,
            "BLOCKED_DRIFT_BEFORE_POST",
            codes=(exc.code,),
            now=pre_post_at,
        )
        return (
            1,
            result_record(
                "BLOCKED_DRIFT_BEFORE_POST",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
                codes=(exc.code,),
            ),
        )
    except Exception:
        journal_transition(
            transaction_id,
            "BLOCKED_DRIFT_BEFORE_POST",
            codes=("POST_RECHECK_UNAVAILABLE",),
            now=pre_post_at,
        )
        return (
            1,
            result_record(
                "BLOCKED_DRIFT_BEFORE_POST",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
                codes=("POST_RECHECK_UNAVAILABLE",),
            ),
        )
    try:
        created = attempt_create(payload)
    except MutationCommandUncertain:
        journal_transition(
            transaction_id,
            "STATE_UNKNOWN_AFTER_POST",
            codes=("DO_NOT_RETRY_POST",),
            now=now_function(),
        )
        return (
            3,
            result_record(
                "STATE_UNKNOWN_AFTER_POST",
                mutation_state="unknown",
                retry_allowed=False,
                rollback_allowed=False,
                codes=("DO_NOT_RETRY_POST",),
            ),
        )
    if not journal_transition(
        transaction_id,
        "CREATED_ID_CONFIRMED",
        ruleset_id=created.ruleset_id,
        now=now_function(),
    ):
        return (
            3,
            result_record(
                "POSTFLIGHT_UNVERIFIED",
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("JOURNAL_UPDATE_FAILED",),
            ),
        )
    try:
        first_assessment = assess_postflight(
            baseline,
            created,
            reviewed_site_sha,
            now=now_function(),
        )
    except Exception:
        journal_transition(
            transaction_id,
            "POSTFLIGHT_UNVERIFIED",
            ruleset_id=created.ruleset_id,
            codes=("INTERNAL_POSTFLIGHT_FAILURE",),
            now=now_function(),
        )
        return (
            3,
            result_record(
                "POSTFLIGHT_UNVERIFIED",
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("INTERNAL_POSTFLIGHT_FAILURE",),
            ),
        )
    if first_assessment.state == "APPLIED_CONFIG_VERIFIED_BEHAVIOR_PENDING":
        if not journal_transition(
            transaction_id,
            first_assessment.state,
            ruleset_id=created.ruleset_id,
            now=now_function(),
        ):
            return (
                3,
                result_record(
                    "JOURNAL_INCOMPLETE_AFTER_CREATE",
                    mutation_state="confirmed_created",
                    retry_allowed=False,
                    rollback_allowed=False,
                    ruleset_id=created.ruleset_id,
                    codes=("JOURNAL_UPDATE_FAILED",),
                ),
            )
        return (
            0,
            result_record(
                first_assessment.state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
            ),
        )
    if first_assessment.state != "POSTFLIGHT_MISMATCH":
        journal_transition(
            transaction_id,
            first_assessment.state,
            ruleset_id=created.ruleset_id,
            codes=first_assessment.codes,
            now=now_function(),
        )
        return (
            3
            if first_assessment.state == "POSTFLIGHT_UNVERIFIED"
            else 1,
            result_record(
                first_assessment.state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=first_assessment.codes,
            ),
        )
    try:
        rollback_now = as_aware_utc(
            now_function(),
            "rollback clock",
        )
        rollback_tick = read_monotonic(
            monotonic_function,
            "rollback monotonic clock",
        )
    except RulesetGuardError:
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_CLOCK_INVALID",),
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_CLOCK_INVALID",),
            ),
        )
    if first_assessment.detail is None:
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_IDENTITY_UNAVAILABLE",),
            now=rollback_now,
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_IDENTITY_UNAVAILABLE",),
            ),
        )
    rollback_identity = first_assessment.detail
    if not rollback_window_is_open(
        post_intent_at,
        rollback_now,
        post_intent_tick,
        rollback_tick,
        previous_at=pre_post_at,
        previous_tick=pre_post_tick,
    ):
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_WINDOW_EXPIRED",),
            now=rollback_now,
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_WINDOW_EXPIRED",),
            ),
        )
    try:
        second_assessment = assess_postflight(
            baseline,
            created,
            reviewed_site_sha,
            now=rollback_now,
        )
    except Exception:
        second_assessment = PostflightAssessment(
            state="POSTFLIGHT_UNVERIFIED",
            codes=("INTERNAL_POSTFLIGHT_FAILURE",),
            snapshot=None,
            detail=None,
        )
    if (
        second_assessment.state != "POSTFLIGHT_MISMATCH"
        or second_assessment.codes != first_assessment.codes
        or second_assessment.snapshot != first_assessment.snapshot
        or second_assessment.detail is None
        or not compare_detail_identity(
            second_assessment.detail,
            rollback_identity,
        )
    ):
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_PRECONDITION_DRIFT",),
            now=now_function(),
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_PRECONDITION_DRIFT",),
            ),
        )
    try:
        delete_intent_at = as_aware_utc(
            now_function(),
            "DELETE intent clock",
        )
        delete_intent_tick = read_monotonic(
            monotonic_function,
            "DELETE intent monotonic clock",
        )
    except RulesetGuardError:
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_CLOCK_INVALID",),
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_CLOCK_INVALID",),
            ),
        )
    if not rollback_window_is_open(
        post_intent_at,
        delete_intent_at,
        post_intent_tick,
        delete_intent_tick,
        previous_at=rollback_now,
        previous_tick=rollback_tick,
    ):
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_WINDOW_EXPIRED_BEFORE_DELETE",),
            now=delete_intent_at,
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_WINDOW_EXPIRED_BEFORE_DELETE",),
            ),
        )
    if not journal_transition(
        transaction_id,
        "DELETE_INTENT_RECORDED",
        ruleset_id=created.ruleset_id,
        codes=second_assessment.codes,
        now=delete_intent_at,
    ):
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("JOURNAL_UPDATE_FAILED",),
            ),
        )
    try:
        immediately_before_delete = as_aware_utc(
            now_function(),
            "pre-DELETE clock",
        )
        pre_delete_tick = read_monotonic(
            monotonic_function,
            "pre-DELETE monotonic clock",
        )
    except RulesetGuardError:
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_CLOCK_INVALID",),
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_CLOCK_INVALID",),
            ),
        )
    if not rollback_window_is_open(
        post_intent_at,
        immediately_before_delete,
        post_intent_tick,
        pre_delete_tick,
        previous_at=delete_intent_at,
        previous_tick=delete_intent_tick,
    ):
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_WINDOW_EXPIRED_BEFORE_DELETE",),
            now=immediately_before_delete,
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_WINDOW_EXPIRED_BEFORE_DELETE",),
            ),
        )
    try:
        final_assessment = assess_postflight(
            baseline,
            created,
            reviewed_site_sha,
            now=immediately_before_delete,
        )
    except Exception:
        final_assessment = PostflightAssessment(
            state="POSTFLIGHT_UNVERIFIED",
            codes=("INTERNAL_POSTFLIGHT_FAILURE",),
            snapshot=None,
            detail=None,
        )
    if (
        final_assessment.state != "POSTFLIGHT_MISMATCH"
        or final_assessment.codes != second_assessment.codes
        or final_assessment.snapshot != second_assessment.snapshot
        or final_assessment.detail is None
        or not compare_detail_identity(
            final_assessment.detail,
            rollback_identity,
        )
    ):
        state = "POSTFLIGHT_MISMATCH_ROLLBACK_INELIGIBLE"
        journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=("ROLLBACK_PRECONDITION_DRIFT",),
            now=immediately_before_delete,
        )
        return (
            1,
            result_record(
                state,
                mutation_state="confirmed_created",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=("ROLLBACK_PRECONDITION_DRIFT",),
            ),
        )
    delete_was_exact = True
    try:
        attempt_delete(created.ruleset_id)
    except MutationCommandUncertain:
        delete_was_exact = False
    try:
        baseline_restored = verify_baseline_restored(
            baseline,
            reviewed_site_sha,
            created.ruleset_id,
            now=now_function(),
        )
    except Exception:
        baseline_restored = False
    if baseline_restored:
        state = "ROLLED_BACK_VERIFIED"
        terminal_codes = (
            ()
            if delete_was_exact
            else ("DELETE_RESULT_RECONCILED_WITHOUT_RETRY",)
        )
        if not journal_transition(
            transaction_id,
            state,
            ruleset_id=created.ruleset_id,
            codes=terminal_codes,
            now=now_function(),
        ):
            return (
                3,
                result_record(
                    "JOURNAL_INCOMPLETE_AFTER_DELETE",
                    mutation_state="confirmed_deleted",
                    retry_allowed=False,
                    rollback_allowed=False,
                    ruleset_id=created.ruleset_id,
                    codes=("JOURNAL_UPDATE_FAILED",),
                ),
            )
        return (
            4,
            result_record(
                state,
                mutation_state="confirmed_deleted",
                retry_allowed=False,
                rollback_allowed=False,
                ruleset_id=created.ruleset_id,
                codes=terminal_codes,
            ),
        )
    state = (
        "ROLLBACK_VERIFY_FAILED"
        if delete_was_exact
        else "STATE_UNKNOWN_AFTER_DELETE"
    )
    journal_transition(
        transaction_id,
        state,
        ruleset_id=created.ruleset_id,
        codes=("DO_NOT_RETRY_DELETE",),
        now=now_function(),
    )
    return (
        3,
        result_record(
            state,
            mutation_state="unknown",
            retry_allowed=False,
            rollback_allowed=False,
            ruleset_id=created.ruleset_id,
            codes=("DO_NOT_RETRY_DELETE",),
        ),
    )


def run_preflight(
    reviewed_site_sha: str,
    reviewed_payload_sha256: str,
) -> tuple[int, dict[str, Any]]:
    try:
        payload = freeze_payload(reviewed_payload_sha256)
        snapshot = perform_preflight(reviewed_site_sha, payload)
    except RulesetGuardError as exc:
        return (
            1,
            result_record(
                exc.code,
                mutation_state="none",
                retry_allowed=True,
                rollback_allowed=False,
            ),
        )
    except Exception:
        return (
            1,
            result_record(
                "BLOCKED_INTERNAL_ERROR",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
            ),
        )
    return (
        0,
        {
            **result_record(
                "PREFLIGHT_VERIFIED",
                mutation_state="none",
                retry_allowed=False,
                rollback_allowed=False,
            ),
            "reviewed_site_sha": reviewed_site_sha,
            "reviewed_payload_sha256": payload.sha256,
            "snapshot_sha256": canonical_digest(snapshot),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("preflight", "apply"):
        command = subparsers.add_parser(mode)
        command.add_argument("--reviewed-site-sha", required=True)
        command.add_argument("--reviewed-payload-sha256", required=True)
        if mode == "apply":
            command.add_argument(
                "--approve-ruleset-application",
                action="store_true",
            )
            command.add_argument(
                "--approve-conditional-rollback",
                action="store_true",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "preflight":
        exit_code, result = run_preflight(
            args.reviewed_site_sha,
            args.reviewed_payload_sha256,
        )
    else:
        exit_code, result = apply_guarded(
            args.reviewed_site_sha,
            args.reviewed_payload_sha256,
            approve_application=args.approve_ruleset_application,
            approve_conditional_rollback=args.approve_conditional_rollback,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
