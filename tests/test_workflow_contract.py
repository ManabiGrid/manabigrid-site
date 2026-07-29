from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
IDENTITY_STEP = (
    "      - name: Verify publication commit identity\n"
    "        run: >-\n"
    "          python3 check_commit_identity.py\n"
    "          --since 96669b41da250a17bd8a0bd77a397dee2af938c1\n"
    '          --commit "${GITHUB_SHA}"\n\n'
)
PROVENANCE_STEP = (
    "      - name: Verify reviewed PR release provenance\n"
    "        env:\n"
    "          GITHUB_TOKEN: ${{ github.token }}\n"
    "        run: >-\n"
    "          python3 check_release_provenance.py\n"
    '          --site-sha "${GITHUB_SHA}"\n'
    '          --ref "${GITHUB_REF}"\n\n'
)


class WorkflowContractTests(unittest.TestCase):
    def run_checker(self, text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "pages.yml"
            fixture.write_text(text, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(ROOT / "check_workflow.py"), str(fixture)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_current_workflow_passes(self) -> None:
        completed = self.run_checker(WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_cron_string_outside_on_schedule_does_not_pass(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        text = re.sub(
            r"  schedule:\n(?:    .*\n)+?  workflow_dispatch:",
            (
                "  workflow_dispatch:\n"
                "    # - cron: \"17 18 * * *\" Asia/Tokyo\n"
            ),
            text,
            count=1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("missing scheduled trigger", completed.stdout)

    def test_commented_deploy_condition_does_not_mask_always(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "    if: needs.build.result == 'success'",
            (
                "    if: always()\n"
                "    # if: needs.build.result == 'success'"
            ),
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("deploy may run after a failed build", completed.stdout)

    def test_echoed_test_command_does_not_count_as_execution(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "        run: python3 -m unittest discover -s tests",
            (
                '        run: echo "python3 -m unittest discover -s tests"\n'
                "        # run: python3 -m unittest discover -s tests"
            ),
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "workflow does not run the provider-independent contract tests",
            completed.stdout,
        )

    def test_annual_schedule_does_not_count_as_daily(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            '- cron: "17 18 * * *"',
            '- cron: "17 18 1 1 *"',
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("schedule must remain the reviewed daily cron", completed.stdout)

    def test_required_test_step_cannot_be_disabled(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "      - name: Run update contract tests\n"
            "        run: python3 -m unittest discover -s tests",
            "      - name: Run update contract tests\n"
            "        if: false\n"
            "        run: python3 -m unittest discover -s tests",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("build step contract changed", completed.stdout)

    def test_publication_identity_step_cannot_be_removed(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            IDENTITY_STEP,
            "",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("detect-source step list or order differs", completed.stdout)

    def test_publication_identity_step_cannot_be_disabled(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "      - name: Verify publication commit identity\n"
            "        run: >-\n"
            "          python3 check_commit_identity.py",
            "      - name: Verify publication commit identity\n"
            "        if: false\n"
            "        run: >-\n"
            "          python3 check_commit_identity.py",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("detect-source step contract changed", completed.stdout)

    def test_publication_identity_command_cannot_be_echoed(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "        run: >-\n"
            "          python3 check_commit_identity.py\n"
            "          --since 96669b41da250a17bd8a0bd77a397dee2af938c1\n"
            '          --commit "${GITHUB_SHA}"',
            '        run: echo "identity check skipped"',
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "Pages does not verify every commit after the privacy baseline",
            completed.stdout,
        )

    def test_publication_identity_range_requires_full_history(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "          fetch-depth: 0\n",
            "",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "publication identity range requires complete site history",
            completed.stdout,
        )

    def test_publication_identity_step_cannot_run_after_source_detection(
        self,
    ) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        text = text.replace(IDENTITY_STEP, "", 1)
        text = text.replace(
            "      - name: Deploy only for a changed source or an explicit site update",
            IDENTITY_STEP
            + "      - name: Deploy only for a changed source or an explicit site update",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("detect-source step list or order differs", completed.stdout)

    def test_release_provenance_step_cannot_be_removed(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            PROVENANCE_STEP,
            "",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("detect-source step list or order differs", completed.stdout)

    def test_release_provenance_step_cannot_be_disabled(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "      - name: Verify reviewed PR release provenance\n"
            "        env:",
            "      - name: Verify reviewed PR release provenance\n"
            "        if: false\n"
            "        env:",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("detect-source step contract changed", completed.stdout)

    def test_release_provenance_must_use_live_github_ref(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            '          --ref "${GITHUB_REF}"',
            "          --ref refs/heads/main",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "Pages does not bind publication to official main",
            completed.stdout,
        )

    def test_release_provenance_permissions_are_read_only_and_complete(
        self,
    ) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "  checks: read\n",
            "",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "top-level permissions are not the reviewed read-only set",
            completed.stdout,
        )

    def test_required_site_check_cannot_continue_on_error(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "      - name: Check the generated static site\n"
            "        run: python3 check_site.py",
            "      - name: Check the generated static site\n"
            "        continue-on-error: true\n"
            "        run: python3 check_site.py",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("build step contract changed", completed.stdout)

    def test_site_check_command_cannot_be_replaced_by_echo(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "        run: python3 check_site.py site-output",
            '        run: echo "python3 check_site.py site-output"',
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("build step contract changed", completed.stdout)

    def test_quarantine_step_cannot_be_disabled(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "      - name: Quarantine the generated public candidate\n"
            "        run: |",
            "      - name: Quarantine the generated public candidate\n"
            "        if: false\n"
            "        run: |",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("build step contract changed", completed.stdout)

    def test_extra_step_between_quarantine_and_package_is_rejected(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "      - name: Package the quarantined site",
            "      - name: Mutate after quarantine\n"
            "        run: echo unsafe > site-output/injected.txt\n\n"
            "      - name: Package the quarantined site",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("build step list or order differs", completed.stdout)

    def test_unnamed_step_after_quarantine_is_rejected(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "      - name: Package the quarantined site",
            "      - run: printf injected > site-output/injected.txt\n\n"
            "      - name: Package the quarantined site",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("build step list or order differs", completed.stdout)

    def test_unnamed_step_before_deploy_is_rejected(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").replace(
            "      - name: Deploy to GitHub Pages",
            "      - run: echo predeploy\n\n"
            "      - name: Deploy to GitHub Pages",
            1,
        )
        completed = self.run_checker(text)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("deploy step list or order differs", completed.stdout)


if __name__ == "__main__":
    unittest.main()
