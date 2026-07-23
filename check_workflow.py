#!/usr/bin/env python3
"""Dry-run structural checks for the GitHub Pages workflow without PyYAML."""

from __future__ import annotations

import argparse
import hashlib
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
EXPECTED_DAILY_CRON = "17 18 * * *"
STEP_CONTRACTS = {
    "detect-source": [
        (
            "Check out the site configuration",
            "69a9b0baac84b75dccc096a75f937b4250d65d8af82aec8017d1fc742da9e6f5",
        ),
        (
            "Read the ManabiGrid main revision",
            "a10cd6776e79bccb9cd80610823b45fd7d05be8c6b237506924ad0097e63f519",
        ),
        (
            "Deploy only for a changed source or an explicit site update",
            "38f4f18d4ee147c05c344a6d48bcb9d54c94f6f82d0c59f6ab3d7820dcc94a01",
        ),
    ],
    "build": [
        (
            "Check out the site repository",
            "98d28918c058e157ff478dca77f6da50deeb67b9adae3a6bfa9aa2ee482438dc",
        ),
        (
            "Check out the canonical ManabiGrid source",
            "1ea5b464a16e4287cd7c5541c28db35752d4b7b39f5e2cf6739a7b21dcf9f86f",
        ),
        (
            "Check the workflow contract",
            "65cbfce6e1c6e8dd60bde94945e3f537daf79296bea71eafa8c4336d6ea0552f",
        ),
        (
            "Run update contract tests",
            "a00562c9c79be9dfe16379ff75e8dea9824d630056d24b371d647ac2b397f485",
        ),
        (
            "Build the static site from the detected source revision",
            "604a15227ff649c33d042c45da8ce8347cc65aae6505803b84e06de0a413e751",
        ),
        (
            "Check the generated static site",
            "b4601805ba4f03e9fb467ba6093eb69d1740179ae84a3f944205d0119c9a5f6e",
        ),
        (
            "Check external links once when manually requested",
            "29a71367ecdddbd50580a20c84db29fe8f8333770077fc33766fa9b69d2ed28c",
        ),
        (
            "Quarantine the generated public candidate",
            "0c519db99c214a26c93cf42e3889c52e718fa43e218fb7d0bc2d807aca7a9345",
        ),
        (
            "Package the quarantined site",
            "1eb194709fe468fd823718f1a02400b8acb6f746cfd9b7a8d70a650cd6d877f9",
        ),
        (
            "Configure GitHub Pages",
            "98dfbf0a0803ba52534fd8dc6e97210743bbd257de9338c2da7dd39a403a7b01",
        ),
        (
            "Upload Pages artifact",
            "841f9886d5a749efbd7c08ed27b925641650276508e944b987eee5dc69275abf",
        ),
    ],
    "deploy": [
        (
            "Deploy to GitHub Pages",
            "00c27dc7799e771575ba7a9b09552371c548f1d7c0f20fbde5a2311eb759f411",
        ),
        (
            "Verify the published revision",
            "307f0ce56ce240f5a6ecccf253b556a6ad2136631424807218536c0144f557fe",
        ),
    ],
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


def yaml_block(lines: list[str], name: str, indent: int) -> str:
    prefix = " " * indent
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(
                rf"{re.escape(prefix)}{re.escape(name)}:\s*",
                line,
            )
        ),
        None,
    )
    if start is None:
        return ""
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip()
            and not lines[index].startswith(prefix + " ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def step_block(job: str, name: str) -> str:
    lines = job.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line == f"      - name: {name}"
        ),
        None,
    )
    if start is None:
        return ""
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("      - ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def require_exact_step_contracts(
    errors: list[str],
    job_name: str,
    job: str,
) -> None:
    """Fail closed when a mandatory step is skipped, weakened, or reordered."""

    expected = STEP_CONTRACTS[job_name]
    observed_names = [
        line.removeprefix("      - name: ")
        for line in job.splitlines()
        if line.startswith("      - name: ")
    ]
    observed_step_starts = [
        line
        for line in job.splitlines()
        if line.startswith("      - ")
    ]
    expected_names = [name for name, _digest in expected]
    require(
        errors,
        observed_names == expected_names
        and len(observed_step_starts) == len(expected_names)
        and all(
            line.startswith("      - name: ")
            for line in observed_step_starts
        ),
        f"{job_name} step list or order differs from the reviewed contract",
    )
    for name, expected_digest in expected:
        block = step_block(job, name)
        observed_digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
        require(
            errors,
            bool(block) and observed_digest == expected_digest,
            (
                f"{job_name} step contract changed: {name}; "
                "review the exact run/uses/env/if/continue-on-error block"
            ),
        )


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
    active_lines = [
        line for line in lines if not line.lstrip().startswith("#")
    ]
    active_text = "\n".join(active_lines)
    on_block = yaml_block(active_lines, "on", 0)

    require(errors, bool(on_block), "missing on trigger block")
    require(errors, bool(re.search(r"^  push:\s*$", on_block, re.MULTILINE)), "missing push trigger")
    require(errors, bool(re.search(r"^    branches: \[main\]\s*$", on_block, re.MULTILINE)), "push is not limited to main")
    require(errors, bool(re.search(r"^  schedule:\s*$", on_block, re.MULTILINE)), "missing scheduled trigger")
    require(errors, bool(re.search(r"^  workflow_dispatch:\s*$", on_block, re.MULTILINE)), "missing workflow_dispatch trigger")
    require(errors, "check_external_links:" in on_block and "type: boolean" in on_block, "manual external-link input is missing")
    require(
        errors,
        bool(
            re.search(
                r"^      source_sha:\n(?:        .*\n)*?        required: true\n(?:        .*\n)*?        type: string\s*$",
                on_block,
                re.MULTILINE,
            )
        )
        and bool(
            re.search(
                r"^      request_id:\n(?:        .*\n)*?        required: true\n(?:        .*\n)*?        type: string\s*$",
                on_block,
                re.MULTILINE,
            )
        ),
        "manual source pin or request correlation input is missing",
    )
    require(
        errors,
        'run-name: "Pages / ${{ github.event_name }} / ${{ inputs.request_id || github.sha }}"' in active_text,
        "run name does not exactly match the request correlation contract",
    )

    cron_match = re.search(r"^    - cron: [\"']([^\"']+)[\"']", on_block, re.MULTILINE)
    require(errors, cron_match is not None, "missing scheduled trigger")
    if cron_match:
        cron = cron_match.group(1)
        fields = cron.split()
        require(errors, len(fields) == 5, "schedule must use five cron fields")
        require(
            errors,
            cron == EXPECTED_DAILY_CRON,
            (
                "schedule must remain the reviewed daily cron "
                f"{EXPECTED_DAILY_CRON!r}"
            ),
        )
        require(errors, "Asia/Tokyo" in text, "schedule must document its Asia/Tokyo conversion")

    require(errors, bool(re.search(r"^permissions:\n  contents: read\s*$", active_text, re.MULTILINE)), "top-level permissions are not minimal contents: read")
    require(errors, bool(re.search(r"^concurrency:\n  group: pages\n  cancel-in-progress: false\s*$", active_text, re.MULTILINE)), "Pages concurrency contract is missing")

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

    source = job_block(active_lines, "detect-source")
    build = job_block(active_lines, "build")
    deploy = job_block(active_lines, "deploy")
    require_exact_step_contracts(errors, "detect-source", source)
    require_exact_step_contracts(errors, "build", build)
    require_exact_step_contracts(errors, "deploy", deploy)
    contract_test_step = step_block(build, "Run update contract tests")
    require(
        errors,
        bool(
            re.search(
                r"^        run: python3 -m unittest discover -s tests\s*$",
                contract_test_step,
                re.MULTILINE,
            )
        ),
        "workflow does not run the provider-independent contract tests",
    )
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
    require(
        errors,
        bool(
            re.search(
                r"^    if: needs\.build\.result == 'success'\s*$",
                deploy,
                re.MULTILINE,
            )
        ),
        "deploy may run after a failed build",
    )
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
    offsets = [active_text.find(marker) for marker in sequence]
    require(errors, all(offset >= 0 for offset in offsets), "build/check/quarantine/package/artifact sequence is incomplete")
    require(errors, offsets == sorted(offsets), "build/check/quarantine/package/artifact order is incorrect")
    require(errors, active_text.find("actions/deploy-pages@") > offsets[-1], "deployment action is not after artifact upload")

    if errors:
        print("Workflow dry-run: FAIL")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    try:
        display_path = workflow.relative_to(ROOT)
    except ValueError:
        display_path = workflow
    print(f"Workflow dry-run: PASS ({display_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
