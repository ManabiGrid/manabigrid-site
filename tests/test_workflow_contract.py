from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"


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
