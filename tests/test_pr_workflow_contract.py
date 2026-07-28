from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

import check_pr_workflow
import negative_css_overflow_check


class PullRequestWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reviewed = check_pr_workflow.DEFAULT_WORKFLOW.read_text(
            encoding="utf-8"
        )

    def check_mutation(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "pr-validate.yml"
            path.write_text(text, encoding="utf-8")
            shutil.copyfile(
                check_pr_workflow.DEFAULT_WORKFLOW.parent / "pages.yml",
                root / "pages.yml",
            )
            return check_pr_workflow.check_workflow(path)

    def test_reviewed_workflow_passes(self) -> None:
        self.assertEqual(check_pr_workflow.check_workflow(), [])

    def test_all_contract_tests_cannot_be_replaced_with_echo(self) -> None:
        mutated = self.reviewed.replace(
            "run: python3 -m unittest discover -s tests -v",
            "run: echo tests-skipped",
        )
        errors = self.check_mutation(mutated)
        self.assertTrue(
            any("unittest discover" in error for error in errors),
            errors,
        )

    def test_device_matrix_cannot_be_disabled_by_false_condition(self) -> None:
        mutated = self.reviewed.replace(
            "      - name: Render all eleven device profiles\n",
            "      - name: Render all eleven device profiles\n"
            "        if: false\n",
        )
        errors = self.check_mutation(mutated)
        self.assertTrue(
            any("false condition" in error for error in errors),
            errors,
        )

    def test_mandatory_check_cannot_continue_on_error(self) -> None:
        mutated = self.reviewed.replace(
            "      - name: Check the fresh static site\n",
            "      - name: Check the fresh static site\n"
            "        continue-on-error: true\n",
        )
        errors = self.check_mutation(mutated)
        self.assertTrue(
            any("continue-on-error" in error for error in errors),
            errors,
        )

    def test_public_quarantine_step_cannot_be_removed(self) -> None:
        start = self.reviewed.index(
            "      - name: Quarantine the generated public candidate\n"
        )
        end = self.reviewed.index(
            "      - name: Render all eleven device profiles\n",
            start,
        )
        errors = self.check_mutation(self.reviewed[:start] + self.reviewed[end:])
        self.assertTrue(
            any("step list or order" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("package_site.py" in error for error in errors),
            errors,
        )

    def test_css_overflow_negative_gate_cannot_be_removed(self) -> None:
        start = self.reviewed.index(
            "      - name: Prove the browser gate rejects CSS page overflow\n"
        )
        end = self.reviewed.index(
            "      - name: Reject canonical source drift during validation\n",
            start,
        )
        errors = self.check_mutation(self.reviewed[:start] + self.reviewed[end:])
        self.assertTrue(
            any("step list or order" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("negative_css_overflow_check.py" in error for error in errors),
            errors,
        )

    def test_final_source_drift_recheck_cannot_be_removed(self) -> None:
        start = self.reviewed.index(
            "      - name: Reject canonical source drift during validation\n"
        )
        errors = self.check_mutation(self.reviewed[:start])
        self.assertTrue(
            any("step list or order" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("observed exactly" in error for error in errors),
            errors,
        )

    def test_pages_write_permission_and_deploy_action_are_rejected(self) -> None:
        mutated = self.reviewed.replace(
            "permissions:\n  contents: read\n",
            "permissions:\n  contents: read\n  pages: write\n  id-token: write\n",
        ).replace(
            (
                "uses: actions/checkout@"
                f"{check_pr_workflow.CHECKOUT_PIN} # v7"
            ),
            (
                "uses: actions/deploy-pages@"
                "cd2ce8fcbc39b97be8ca5fce6e763baed58fa128 # v5"
            ),
            1,
        )
        errors = self.check_mutation(mutated)
        self.assertTrue(any("pages:" in error for error in errors), errors)
        self.assertTrue(
            any("actions/deploy-pages" in error for error in errors),
            errors,
        )

    def test_second_workflow_cannot_shadow_the_required_check_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = root / "pr-validate.yml"
            reviewed.write_text(self.reviewed, encoding="utf-8")
            (root / "shadow.yml").write_text(
                "name: shadow\n"
                "on: pull_request\n"
                "jobs:\n"
                "  shadow:\n"
                f"    name: {check_pr_workflow.REQUIRED_CHECK_NAME}\n"
                "    runs-on: ubuntu-24.04\n"
                "    steps:\n"
                "      - run: echo bypass\n",
                encoding="utf-8",
            )
            errors = check_pr_workflow.check_workflow(reviewed)
        self.assertTrue(
            any("shadow the required check" in error for error in errors),
            errors,
        )

    def test_quoted_job_name_in_reviewed_workflow_set_cannot_shadow_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = root / "pr-validate.yml"
            reviewed.write_text(self.reviewed, encoding="utf-8")
            pages = (
                check_pr_workflow.DEFAULT_WORKFLOW.parent / "pages.yml"
            ).read_text(encoding="utf-8")
            (root / "pages.yml").write_text(
                pages.replace(
                    "    name: Detect canonical source revision\n",
                    (
                        "    name: "
                        f'"{check_pr_workflow.REQUIRED_CHECK_NAME}"\n'
                    ),
                    1,
                ),
                encoding="utf-8",
            )
            errors = check_pr_workflow.check_workflow(reviewed)
        self.assertTrue(
            any("shadow the required check" in error for error in errors),
            errors,
        )

    def test_expression_job_name_in_other_workflow_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed = root / "pr-validate.yml"
            reviewed.write_text(self.reviewed, encoding="utf-8")
            pages = (
                check_pr_workflow.DEFAULT_WORKFLOW.parent / "pages.yml"
            ).read_text(encoding="utf-8")
            (root / "pages.yml").write_text(
                pages.replace(
                    "    name: Detect canonical source revision\n",
                    "    name: ${{ 'computed-check-name' }}\n",
                    1,
                ),
                encoding="utf-8",
            )
            errors = check_pr_workflow.check_workflow(reviewed)
        self.assertTrue(
            any("expression-based job name" in error for error in errors),
            errors,
        )


class CssOverflowNegativeGateTests(unittest.TestCase):
    def valid_report(self) -> dict[str, object]:
        return {
            "status": "failed",
            "errors": [negative_css_overflow_check.EXPECTED_ERROR],
            "pages": [
                {
                    "label": f"page-{index}",
                    "errors": [negative_css_overflow_check.EXPECTED_ERROR],
                }
                for index in range(
                    negative_css_overflow_check.EXPECTED_RENDERED_PAGES
                )
            ],
        }

    def test_every_reviewed_page_must_reject_overflow(self) -> None:
        report = self.valid_report()
        self.assertEqual(
            negative_css_overflow_check.validate_negative_report(report),
            [],
        )
        pages = report["pages"]
        assert isinstance(pages, list)
        pages[0]["errors"] = []
        errors = negative_css_overflow_check.validate_negative_report(report)
        self.assertTrue(
            any("page-0: overflow error" in error for error in errors),
            errors,
        )

    def test_page_count_and_labels_fail_closed(self) -> None:
        report = self.valid_report()
        pages = report["pages"]
        assert isinstance(pages, list)
        pages.pop()
        errors = negative_css_overflow_check.validate_negative_report(report)
        self.assertTrue(
            any("17ページ" in error for error in errors),
            errors,
        )

        report = self.valid_report()
        pages = report["pages"]
        assert isinstance(pages, list)
        pages[1]["label"] = pages[0]["label"]
        errors = negative_css_overflow_check.validate_negative_report(report)
        self.assertTrue(
            any("labelが重複" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
