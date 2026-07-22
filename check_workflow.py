#!/usr/bin/env python3
"""Dry-run structural checks for the GitHub Pages workflow without PyYAML."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
ACTION_PINS = {
    "actions/checkout": ("9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0", "v7"),
    "actions/configure-pages": ("45bfe0192ca1faeb007ade9deae92b16b8254a0d", "v6"),
    "actions/upload-pages-artifact": ("fc324d3547104276b827a68afc52ff2a11cc49c9", "v5"),
    "actions/deploy-pages": ("cd2ce8fcbc39b97be8ca5fce6e763baed58fa128", "v5"),
}


def job_block(lines: list[str], name: str) -> str:
    start = next(
        (index for index, line in enumerate(lines) if re.fullmatch(rf"  {re.escape(name)}:\s*", line)),
        None,
    )
    if start is None:
        return ""
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.fullmatch(r"  [A-Za-z0-9_-]+:\s*", lines[index])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", nargs="?", type=Path, default=DEFAULT_WORKFLOW)
    args = parser.parse_args()
    workflow = args.workflow.resolve()
    errors: list[str] = []

    try:
        text = workflow.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Workflow dry-run: FAIL\n- cannot read {workflow}: {exc}")
        return 1
    lines = text.splitlines()

    require(errors, bool(re.search(r"^on:\s*$", text, re.MULTILINE)), "missing on trigger block")
    require(errors, bool(re.search(r"^  push:\s*$", text, re.MULTILINE)), "missing push trigger")
    require(errors, bool(re.search(r"^    branches: \[main\]\s*$", text, re.MULTILINE)), "push is not limited to main")
    require(errors, bool(re.search(r"^  workflow_dispatch:\s*$", text, re.MULTILINE)), "missing workflow_dispatch trigger")
    require(errors, "check_external_links:" in text and "type: boolean" in text, "manual external-link input is missing")
    require(
        errors,
        bool(
            re.search(
                r"^      source_sha:\n(?:        .*\n)*?        required: true\n(?:        .*\n)*?        type: string\s*$",
                text,
                re.MULTILINE,
            )
        )
        and bool(
            re.search(
                r"^      request_id:\n(?:        .*\n)*?        required: true\n(?:        .*\n)*?        type: string\s*$",
                text,
                re.MULTILINE,
            )
        ),
        "manual source pin or request correlation input is missing",
    )
    require(
        errors,
        'run-name: "Pages / ${{ github.event_name }} / ${{ inputs.request_id || github.sha }}"' in text,
        "run name does not exactly match the request correlation contract",
    )
    require(
        errors,
        "- name: Run update contract tests" in text
        and "python3 -m unittest discover -s tests" in text,
        "workflow does not run the provider-independent contract tests",
    )

    cron_match = re.search(r"^\s*- cron: [\"']([^\"']+)[\"']", text, re.MULTILINE)
    require(errors, cron_match is not None, "missing scheduled trigger")
    if cron_match:
        fields = cron_match.group(1).split()
        require(errors, len(fields) == 5, "schedule must use five cron fields")
        if fields:
            require(errors, fields[0] not in {"0", "00"}, "schedule must avoid minute zero")
        require(errors, "Asia/Tokyo" in text, "schedule must document its Asia/Tokyo conversion")

    require(errors, bool(re.search(r"^permissions:\n  contents: read\s*$", text, re.MULTILINE)), "top-level permissions are not minimal contents: read")
    require(errors, bool(re.search(r"^concurrency:\n  group: pages\n  cancel-in-progress: false\s*$", text, re.MULTILINE)), "Pages concurrency contract is missing")

    uses_lines = [line.strip() for line in lines if re.match(r"\s*uses:\s+", line)]
    for use in uses_lines:
        match = re.fullmatch(r"uses:\s+([^@\s]+)@([0-9a-f]{40})(?:\s+#\s*(v\d+))?", use)
        require(errors, match is not None, f"action is not pinned to a 40-character SHA: {use}")
        if not match:
            continue
        action, sha, major = match.groups()
        expected = ACTION_PINS.get(action)
        if expected:
            require(errors, sha == expected[0], f"{action} SHA does not resolve {expected[1]}")
            require(errors, major == expected[1], f"{action} major-version comment is missing")
    for action in ACTION_PINS:
        require(errors, any(line.startswith(f"uses: {action}@") for line in uses_lines), f"missing required action {action}")

    source = job_block(lines, "detect-source")
    build = job_block(lines, "build")
    deploy = job_block(lines, "deploy")
    require(errors, "ManabiGrid/manabigrid.git refs/heads/main" in source, "canonical source update detection is missing")
    require(
        errors,
        "APPROVED_SOURCE_SHA" in source
        and "approved source drifted" in source
        and "workflow_dispatch" in source,
        "manual run does not reject drift from the approved source SHA",
    )
    require(
        errors,
        "should_deploy:" in source
        and 'Path("site.config.json")' in source
        and 'config["base_url"].rstrip("/") + "/build-report.json"' in source,
        "published source revision comparison is not derived from site.config.json",
    )
    require(
        errors,
        "https://manabigrid.github.io/manabigrid-site/build-report.json" not in source,
        "detect-source hard-codes the published build-report URL",
    )
    require(errors, "repository: ManabiGrid/manabigrid" in build, "canonical source checkout is missing")
    require(errors, "ref: ${{ needs.detect-source.outputs.source_sha }}" in build, "build does not use the detected source SHA")
    require(
        errors,
        bool(
            re.search(
                r"repository: ManabiGrid/manabigrid[\s\S]{0,400}?fetch-depth: 0",
                build,
            )
        ),
        "canonical source checkout is shallow; update history would be incomplete",
    )
    require(errors, "if: needs.detect-source.outputs.should_deploy == 'true'" in build, "unchanged scheduled source is not skipped")
    require(
        errors,
        '--expected-source-sha "${{ needs.detect-source.outputs.source_sha }}"' in build
        and 'MANABIGRID_SITE_COMMIT_SHA: ${{ github.sha }}' in build
        and '--expected-site-sha "${{ github.sha }}"' in build,
        "build/check does not pin canonical and site commit provenance",
    )
    require(errors, "check_external_links.py site-output --run" in build, "one-time external link checker is not wired")
    require(
        errors,
        "needs: build" in deploy or "needs: [detect-source, build]" in deploy,
        "deploy does not depend on build",
    )
    require(errors, "if: needs.build.result == 'success'" in deploy, "deploy may run after a failed build")
    require(errors, "pages: write" in deploy and "id-token: write" in deploy, "deploy permissions are incomplete")
    require(
        errors,
        "Verify the published revision" in deploy
        and "EXPECTED_SOURCE_SHA" in deploy
        and "EXPECTED_SITE_SHA" in deploy
        and 'report.get("publication", {}).get("site_commit")' in deploy
        and "build-report.json" in deploy
        and "missing page did not return HTTP 404" in deploy,
        "post-deploy published SHA / HTTP verification is incomplete",
    )

    sequence = ["build_site.py", "check_site.py", "Quarantine the generated public candidate", "package_site.py", "upload-pages-artifact"]
    offsets = [text.find(marker) for marker in sequence]
    require(errors, all(offset >= 0 for offset in offsets), "build/check/quarantine/package/artifact sequence is incomplete")
    require(errors, offsets == sorted(offsets), "build/check/quarantine/package/artifact order is incorrect")
    require(errors, text.find("actions/deploy-pages@") > offsets[-1], "deployment action is not after artifact upload")

    if errors:
        print("Workflow dry-run: FAIL")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"Workflow dry-run: PASS ({workflow.relative_to(ROOT)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
