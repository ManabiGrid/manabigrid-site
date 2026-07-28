#!/usr/bin/env python3
"""Fail-closed contract checker for the non-deploying PR validation workflow."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_WORKFLOW = ROOT / ".github" / "workflows" / "pr-validate.yml"
REVIEWED_WORKFLOW_SHA256 = (
    "926ec32178d695033a7c6ae5700e663bbcdea195ef5418027366be89922f6d46"
)
CHECKOUT_PIN = "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
EXPECTED_STEPS = (
    "Check out the reviewed site revision",
    "Read the canonical ManabiGrid source revision",
    "Check out the canonical source at the observed revision",
    "Check both workflow contracts",
    "Run every contract test",
    "Build a fresh static site",
    "Check the fresh static site",
    "Quarantine the generated public candidate",
    "Render all eleven device profiles",
    "Prove the browser gate rejects CSS page overflow",
    "Reject canonical source drift during validation",
)
REQUIRED_CHECK_NAME = "manabigrid-site-pr-gate"
EXPECTED_WORKFLOW_FILES = frozenset({"pages.yml", "pr-validate.yml"})


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def check_workflow(path: Path = DEFAULT_WORKFLOW) -> list[str]:
    errors: list[str] = []
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read PR workflow: {exc}"]

    digest = hashlib.sha256(raw).hexdigest()
    require(
        errors,
        digest == REVIEWED_WORKFLOW_SHA256,
        (
            "PR workflow differs from the reviewed byte-for-byte contract; "
            "review the complete gate before updating its digest"
        ),
    )

    active_lines = [
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ]
    active = "\n".join(active_lines)
    observed_steps = tuple(
        line.removeprefix("      - name: ")
        for line in active_lines
        if line.startswith("      - name: ")
    )
    all_step_starts = [
        line for line in active_lines if line.startswith("      - ")
    ]
    workflow_texts: dict[Path, str] = {}
    try:
        workflow_paths = sorted(path.parent.glob("*.yml")) + sorted(
            path.parent.glob("*.yaml")
        )
        workflow_texts = {
            workflow_path: workflow_path.read_text(encoding="utf-8")
            for workflow_path in workflow_paths
        }
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"cannot scan workflow names: {exc}")
    require(
        errors,
        {workflow_path.name for workflow_path in workflow_texts}
        == EXPECTED_WORKFLOW_FILES,
        (
            "workflow file set differs from the reviewed allowlist: "
            f"{sorted(EXPECTED_WORKFLOW_FILES)}"
        ),
    )

    require(
        errors,
        re.search(
            r"^on:\n  pull_request:\n    branches: \[main\]\s*$",
            active,
            re.MULTILINE,
        )
        is not None,
        "PR trigger must be limited to pull_request targeting main",
    )
    for forbidden_trigger in (
        "pull_request_target:",
        "push:",
        "schedule:",
        "workflow_dispatch:",
    ):
        require(
            errors,
            forbidden_trigger not in active,
            f"PR workflow must not add trigger {forbidden_trigger}",
        )
    require(
        errors,
        re.search(
            r"^permissions:\n  contents: read\s*$",
            active,
            re.MULTILINE,
        )
        is not None,
        "top-level permissions must remain exactly contents: read",
    )
    for forbidden_permission in ("pages:", "id-token:", "actions:", "checks:"):
        require(
            errors,
            forbidden_permission not in active,
            f"PR workflow must not request {forbidden_permission} permission",
        )
    require(
        errors,
        f"name: {REQUIRED_CHECK_NAME}" in active,
        f"required check name must remain exactly {REQUIRED_CHECK_NAME}",
    )
    require(
        errors,
        re.search(
            rf"^    name:\s+{re.escape(REQUIRED_CHECK_NAME)}\s*$",
            active,
            re.MULTILINE,
        )
        is not None,
        f"required check name must be the literal job name {REQUIRED_CHECK_NAME}",
    )
    shadowing_job_names: list[str] = []
    for workflow_path, workflow_text in workflow_texts.items():
        if workflow_path.resolve() == path.resolve():
            continue
        if REQUIRED_CHECK_NAME in workflow_text:
            shadowing_job_names.append(
                f"{workflow_path.name} contains the required check name"
            )
        for line in workflow_text.splitlines():
            match = re.match(r"^    name:\s*(.+?)\s*$", line)
            if match and "${{" in match.group(1):
                shadowing_job_names.append(
                    f"{workflow_path.name} has an expression-based job name"
                )
    require(
        errors,
        not shadowing_job_names,
        (
            "other workflows must not shadow the required check through a "
            "literal, quoted, or expression-based job name: "
            + "; ".join(shadowing_job_names)
        ),
    )
    require(
        errors,
        "runs-on: ubuntu-24.04" in active,
        "PR validation must use the reviewed ubuntu-24.04 runner image",
    )
    require(
        errors,
        "timeout-minutes: 30" in active,
        "PR validation timeout must remain 30 minutes",
    )
    require(
        errors,
        observed_steps == EXPECTED_STEPS
        and len(all_step_starts) == len(EXPECTED_STEPS)
        and all(line.startswith("      - name: ") for line in all_step_starts),
        "mandatory PR gate step list or order changed",
    )

    uses = [
        line.strip()
        for line in active_lines
        if re.match(r"\s*uses:\s+", line)
    ]
    require(
        errors,
        uses
        == [
            f"uses: actions/checkout@{CHECKOUT_PIN} # v7",
            f"uses: actions/checkout@{CHECKOUT_PIN} # v7",
        ],
        "only two SHA-pinned checkout actions are allowed",
    )
    for forbidden_action in (
        "actions/configure-pages",
        "actions/upload-pages-artifact",
        "actions/deploy-pages",
    ):
        require(
            errors,
            forbidden_action not in active,
            f"PR workflow must not invoke {forbidden_action}",
        )

    require(
        errors,
        "continue-on-error:" not in active,
        "mandatory gates must not use continue-on-error",
    )
    require(
        errors,
        re.search(
            r"^\s+if:\s*(?:false|\$\{\{\s*false\s*\}\})\s*$",
            active,
            re.MULTILINE,
        )
        is None,
        "mandatory gates must not be disabled with a false condition",
    )
    require(
        errors,
        "repository: ManabiGrid/manabigrid" in active
        and "ref: ${{ steps.source.outputs.sha }}" in active
        and "path: source" in active
        and "fetch-depth: 0" in active,
        "canonical source checkout must be pinned to the observed main SHA",
    )
    source_observation = (
        "git ls-remote https://github.com/ManabiGrid/manabigrid.git "
        "refs/heads/main"
    )
    require(
        errors,
        active.count(source_observation) == 2,
        (
            "canonical source SHA must be observed exactly at the start and "
            "rechecked after every validation gate"
        ),
    )
    mandatory_commands = (
        "python3 check_workflow.py",
        "python3 check_pr_workflow.py",
        "python3 -m unittest discover -s tests -v",
        (
            "python3 build_site.py --source source --output site-output "
            '--no-check --expected-source-sha "${{ steps.source.outputs.sha }}"'
        ),
        (
            "python3 check_site.py site-output --source source "
            '--expected-source-sha "${{ steps.source.outputs.sha }}" '
            '--expected-site-sha "${{ github.sha }}"'
        ),
        "python3 package_site.py --site-root site-output --dry-run",
        "python3 device_matrix_check.py --site-root site-output",
        "python3 negative_css_overflow_check.py --site-root site-output",
        'if [[ "${latest_source_sha}" != "${INITIAL_SOURCE_SHA}" ]]; then',
    )
    for command in mandatory_commands:
        require(
            errors,
            command in active,
            f"mandatory PR gate command changed or is missing: {command}",
        )
    for token in (
        '"/Users/"',
        '"/home/runner/"',
        '"manabigrid_public_staging"',
        '"INTERNAL_ONLY"',
    ):
        require(
            errors,
            token in active,
            f"public candidate quarantine token is missing: {token}",
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", nargs="?", type=Path, default=DEFAULT_WORKFLOW)
    args = parser.parse_args()
    errors = check_workflow(args.workflow.resolve())
    if errors:
        print("PR workflow contract: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PR workflow contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
