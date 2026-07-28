from __future__ import annotations

from collections import defaultdict
import tempfile
import unittest
from pathlib import Path

import build_site
import browser_check
import check_site


class InlineMathBuildTests(unittest.TestCase):
    def test_translucent_math_color_is_composited_for_contrast(self) -> None:
        ratio = browser_check.contrast_ratio(
            "rgba(243, 246, 252, 0.78)",
            "rgb(16, 24, 39)",
        )
        self.assertGreater(ratio, 4.5)

    @staticmethod
    def renderer_for(rel: Path) -> build_site.Markdown:
        source = Path("/source")
        doc = build_site.Doc(
            path=source / rel,
            rel=rel,
            output=build_site.output_for(rel),
            kind="lesson",
            title="test",
            subject="jhs-math-2",
            unit="test",
            sha256="0" * 64,
            frontmatter=False,
            tags=0,
        )
        stats = build_site.Stats()
        resolver = build_site.Resolver(source, [doc], {}, {}, stats)
        return build_site.Markdown(doc, resolver, stats)

    def test_approved_literals_render_native_inline_mathml(self) -> None:
        for trial in build_site.INLINE_MATH_TRIALS:
            with self.subTest(trial=trial.trial_id):
                rendered = self.renderer_for(trial.source).inline(trial.literal)
                self.assertEqual(rendered.count("<math "), 1)
                self.assertIn(
                    '<span class="inline-math-scroll" '
                    'data-scroll-label="数式を横にスクロール">',
                    rendered,
                )
                self.assertIn('<span class="inline-math-shell">', rendered)
                self.assertIn("↔ 数式は左右に動かせます", rendered)
                self.assertIn('class="inline-math"', rendered)
                self.assertIn('display="inline"', rendered)
                self.assertIn(
                    f'data-math-id="{trial.trial_id}"',
                    rendered,
                )
                self.assertIn(
                    f'aria-label="{trial.aria_label}"',
                    rendered,
                )
                self.assertIn("<semantics>", rendered)
                self.assertIn(
                    '<annotation encoding="text/plain">',
                    rendered,
                )
                self.assertIn(trial.literal, rendered)

    def test_fraction_trials_use_mfrac_and_operator_roles(self) -> None:
        rendered = {
            trial.trial_id: self.renderer_for(trial.source).inline(trial.literal)
            for trial in build_site.INLINE_MATH_TRIALS
        }
        self.assertEqual(rendered["signed-numeric-fractions"].count("<mfrac>"), 2)
        self.assertEqual(rendered["variable-fraction"].count("<mfrac>"), 1)
        self.assertIn("<mi>x</mi>", rendered["x-times-evaluation"])
        self.assertIn("<mo>×</mo>", rendered["x-times-evaluation"])
        self.assertIn("<mo>+</mo>", rendered["signed-addition"])
        self.assertIn("<mo>−</mo>", rendered["signed-addition"])

    def test_code_link_and_image_alt_are_not_transformed(self) -> None:
        trial = build_site.INLINE_MATH_TRIALS[0]
        renderer = self.renderer_for(trial.source)
        samples = (
            f"`{trial.literal}`",
            f"[{trial.literal}](https://example.invalid/)",
            f"![{trial.literal}](missing.svg)",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertNotIn("<math", renderer.inline(sample))

    def test_markdown_emphasis_can_wrap_an_approved_expression(self) -> None:
        trial = build_site.INLINE_MATH_TRIALS[0]
        rendered = self.renderer_for(trial.source).inline(f"**{trial.literal}**")
        self.assertIn('<strong><span class="inline-math-shell"', rendered)
        self.assertIn("</math></span>", rendered)
        self.assertIn("</span></strong>", rendered)

    def test_url_ratio_date_and_wrong_answer_mark_remain_plain_text(self) -> None:
        renderer = self.renderer_for(build_site.INLINE_MATH_TRIALS[1].source)
        negatives = (
            "https://www.mext.go.jp/b_menu/1351168.htm",
            "1:1:√2",
            "2026-07-20",
            "3×2＋3＝9 ×",
        )
        for value in negatives:
            with self.subTest(value=value):
                rendered = renderer.inline(value)
                self.assertNotIn("<math", rendered)
                self.assertIn(value, rendered)

    def test_same_literal_on_an_unapproved_page_remains_plain_text(self) -> None:
        trial = build_site.INLINE_MATH_TRIALS[0]
        rendered = self.renderer_for(
            Path("materials/jhs-math-2/example/lesson_01.md")
        ).inline(trial.literal)
        self.assertNotIn("<math", rendered)
        self.assertIn(trial.literal, rendered)

    def test_source_registry_requires_exact_occurrence_counts(self) -> None:
        by_source: dict[Path, list[str]] = defaultdict(list)
        for trial in build_site.INLINE_MATH_TRIALS:
            by_source[trial.source].extend(
                [trial.literal] * trial.expected_occurrences
            )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            for rel, literals in by_source.items():
                path = source / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(literals), encoding="utf-8")
            build_site.validate_inline_math_source_registry(source)

            first = build_site.INLINE_MATH_TRIALS[0]
            path = source / first.source
            path.write_text(
                path.read_text(encoding="utf-8").replace(first.literal, "変更", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                build_site.BuildError,
                "完全一致件数",
            ):
                build_site.validate_inline_math_source_registry(source)


class InlineMathCheckTests(unittest.TestCase):
    @staticmethod
    def build_report() -> dict[str, object]:
        return {
            "features": {
                "mathml_static_prototypes": 1,
                "inline_math_rendered": sum(
                    item.count for item in check_site.INLINE_MATH_EXPECTATIONS
                ),
                "inline_math_trials": [
                    {
                        "id": item.trial_id,
                        "source": item.source,
                        "output": item.output,
                        "expression": item.annotation,
                        "aria_label": item.aria_label,
                        "expected_occurrences": item.count,
                        "rendered_occurrences": item.count,
                    }
                    for item in check_site.INLINE_MATH_EXPECTATIONS
                ],
            }
        }

    @staticmethod
    def write_valid_pages(site_root: Path) -> dict[Path, check_site.ParsedHtml]:
        build_trials = {
            trial.trial_id: trial for trial in build_site.INLINE_MATH_TRIALS
        }
        fragments: dict[str, list[str]] = defaultdict(list)
        for expectation in check_site.INLINE_MATH_EXPECTATIONS:
            fragments[expectation.output].extend(
                [build_site.render_inline_math(build_trials[expectation.trial_id])]
                * expectation.count
            )
        fragments[check_site.DISPLAY_MATH_EXPECTATION].append(
            build_site.mathml_prototype(build_site.MATHML_PROTOTYPE_EXPRESSION)
        )
        parsed: dict[Path, check_site.ParsedHtml] = {}
        for relative, nodes in fragments.items():
            path = site_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "<!doctype html><html><body><main>"
                + "".join(nodes)
                + "</main></body></html>",
                encoding="utf-8",
            )
            parsed[path] = check_site.HtmlCollector(path).load()
        return parsed

    def test_checker_accepts_only_the_fixed_three_page_trial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site_root = Path(temporary)
            parsed = self.write_valid_pages(site_root)
            errors = check_site.inline_math_contract_errors(
                site_root,
                parsed,
                self.build_report(),
            )
        self.assertEqual(errors, [])

    def test_checker_rejects_missing_unapproved_or_malformed_mathml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site_root = Path(temporary)
            parsed = self.write_valid_pages(site_root)
            target = site_root / check_site.INLINE_MATH_EXPECTATIONS[0].output
            target.write_text(
                target.read_text(encoding="utf-8").replace(
                    'data-math-id="x-times-evaluation"',
                    'data-math-id="unapproved"',
                    1,
                ),
                encoding="utf-8",
            )
            parsed[target] = check_site.HtmlCollector(target).load()
            errors = check_site.inline_math_contract_errors(
                site_root,
                parsed,
                self.build_report(),
            )
        self.assertTrue(any("unapproved inline MathML" in error for error in errors))
        self.assertTrue(any("count mismatch" in error for error in errors))

    def test_checker_rejects_mathml_presentation_tree_mutations(self) -> None:
        mutations = (
            (
                "variable x role change",
                check_site.INLINE_MATH_EXPECTATIONS[0].output,
                "<mi>x</mi>",
                "<mo>x</mo>",
                "inline MathML semantics mismatch",
            ),
            (
                "multiplication sign role change",
                check_site.INLINE_MATH_EXPECTATIONS[0].output,
                "<mo>×</mo>",
                "<mi>×</mi>",
                "inline MathML semantics mismatch",
            ),
            (
                "numeric fraction inversion",
                check_site.INLINE_MATH_EXPECTATIONS[2].output,
                "<mfrac><mn>2</mn><mn>3</mn></mfrac>",
                "<mfrac><mn>3</mn><mn>2</mn></mfrac>",
                "inline MathML semantics mismatch",
            ),
            (
                "result sign change",
                check_site.INLINE_MATH_EXPECTATIONS[1].output,
                "<mo>=</mo><mo>−</mo><mn>3</mn>",
                "<mo>=</mo><mo>+</mo><mn>3</mn>",
                "inline MathML semantics mismatch",
            ),
            (
                "display fraction inversion",
                check_site.DISPLAY_MATH_EXPECTATION,
                "<mfrac><mn>1</mn><mn>2</mn></mfrac>",
                "<mfrac><mn>2</mn><mn>1</mn></mfrac>",
                "display MathML prototype semantics mismatch",
            ),
            (
                "unknown operator attribute",
                check_site.INLINE_MATH_EXPECTATIONS[0].output,
                "<mo>+</mo>",
                '<mo data-unknown="1">+</mo>',
                "inline MathML semantics mismatch",
            ),
        )
        for name, relative, before, after, expected_error in mutations:
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as temporary:
                site_root = Path(temporary)
                parsed = self.write_valid_pages(site_root)
                target = site_root / relative
                original = target.read_text(encoding="utf-8")
                self.assertIn(before, original)
                target.write_text(
                    original.replace(before, after, 1),
                    encoding="utf-8",
                )
                parsed[target] = check_site.HtmlCollector(target).load()
                errors = check_site.inline_math_contract_errors(
                    site_root,
                    parsed,
                    self.build_report(),
                )
                self.assertTrue(
                    any(expected_error in error for error in errors),
                    errors,
                )

    def test_checker_rejects_report_self_claim_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site_root = Path(temporary)
            parsed = self.write_valid_pages(site_root)
            report = self.build_report()
            features = report["features"]
            assert isinstance(features, dict)
            features["inline_math_rendered"] = 999
            errors = check_site.inline_math_contract_errors(
                site_root,
                parsed,
                report,
            )
        self.assertIn(
            "build-report inline MathML rendered count mismatch",
            errors,
        )

    def test_checker_rejects_bool_and_float_report_counts(self) -> None:
        mutations = (
            (
                "inline rendered float",
                lambda features: features.__setitem__("inline_math_rendered", 5.0),
                "build-report inline MathML rendered count mismatch",
            ),
            (
                "display count bool",
                lambda features: features.__setitem__(
                    "mathml_static_prototypes",
                    True,
                ),
                "build-report display MathML prototype count mismatch",
            ),
            (
                "trial expected count bool",
                lambda features: features["inline_math_trials"][0].__setitem__(
                    "expected_occurrences",
                    True,
                ),
                "build-report inline MathML trial details mismatch",
            ),
            (
                "trial rendered count float",
                lambda features: features["inline_math_trials"][0].__setitem__(
                    "rendered_occurrences",
                    1.0,
                ),
                "build-report inline MathML trial details mismatch",
            ),
        )
        for name, mutate, expected_error in mutations:
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as temporary:
                site_root = Path(temporary)
                parsed = self.write_valid_pages(site_root)
                report = self.build_report()
                features = report["features"]
                assert isinstance(features, dict)
                mutate(features)
                errors = check_site.inline_math_contract_errors(
                    site_root,
                    parsed,
                    report,
                )
                self.assertIn(expected_error, errors)


if __name__ == "__main__":
    unittest.main()
