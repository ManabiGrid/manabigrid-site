from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import check_site


ENDPOINT = "https://script.google.com/macros/s/test_deployment-123/exec"


class RatingGateTests(unittest.TestCase):
    @staticmethod
    def script() -> str:
        source = Path(check_site.__file__).with_name("static") / "rating.js"
        return source.read_text(encoding="utf-8").replace(
            '"__RATING_ENDPOINT__"', json.dumps(ENDPOINT)
        )

    @staticmethod
    def markup(unit: str = "jhs-math-example", lesson: str = "L01") -> str:
        return (
            '<html><head><link rel="stylesheet" href="../../../../_assets/rating.css">'
            '</head><body><main><article class="lesson-main">'
            f'<section class="lesson-rating" data-rating-unit="{unit}" '
            f'data-rating-lesson="{lesson}" '
            f'data-rating-feedback-url="{check_site.RATING_FEEDBACK_URL}" '
            'hidden aria-label="このレッスンはどうでしたか">'
            f'<p class="rating-count" data-rating-count-unit="{unit}" hidden></p>'
            '</section></article></main>'
            '<script src="../../../../_assets/rating.js" defer></script></body></html>'
        )

    def html_errors(self, raw: str, endpoint: str = ENDPOINT) -> list[str]:
        return check_site.rating_html_contract_errors(
            "content/materials/jhs-math/jhs-math-example/lesson_01.html",
            raw,
            endpoint,
            ("jhs-math-example", "L01"),
            "jhs-math-example",
        )

    def test_endpoint_allows_only_exact_apps_script_exec_url(self) -> None:
        self.assertEqual(check_site.validate_rating_endpoint(""), "")
        self.assertEqual(check_site.validate_rating_endpoint(ENDPOINT), ENDPOINT)
        for value in (
            None, 1, False, " ", " " + ENDPOINT, ENDPOINT + " ",
            ENDPOINT + "?unit=test", ENDPOINT + "#fragment",
            ENDPOINT.replace("/exec", "/dev"),
            ENDPOINT.replace("https:", "http:"),
            ENDPOINT.replace("script.google.com", "script.google.com.evil.example"),
            ENDPOINT.replace("script.google.com", "script.googleusercontent.com"),
            ENDPOINT.replace("script.google.com", "script.google.com:443"),
            ENDPOINT.replace("/s/test_deployment-123/", "/s/test/deployment/"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    check_site.validate_rating_endpoint(value)

    def test_exact_rating_script_is_accepted(self) -> None:
        self.assertEqual(
            check_site.javascript_privacy_contract_errors(
                "_assets/rating.js", self.script(), ENDPOINT
            ),
            [],
        )

    def test_fetch_remains_forbidden_in_every_other_script_and_when_disabled(self) -> None:
        for relative, endpoint in (
            ("_assets/site.js", ENDPOINT),
            ("_assets/copied/rating.js", ENDPOINT),
            ("content/rating.js", ENDPOINT),
            ("_assets/rating.js", ""),
        ):
            with self.subTest(relative=relative, endpoint=endpoint):
                self.assertTrue(check_site.javascript_privacy_contract_errors(
                    relative, self.script(), endpoint
                ))
        self.assertTrue(check_site.javascript_privacy_contract_errors(
            "_assets/site.js", "fetch ('https://example.com');", ENDPOINT
        ))

    def test_rating_rejects_other_endpoint_host_or_request_destination(self) -> None:
        script = self.script()
        changed = (
            script.replace(ENDPOINT, "https://example.com/exec"),
            script.replace("test_deployment-123", "different_deployment"),
            script.replace("fetch(ENDPOINT,", "fetch('https://example.com/exec',"),
            script.replace("'?unit='", "'?different='"),
            script + "\nfetch(ENDPOINT, {});",
            script + "\nwindow['fetch'](ENDPOINT);",
        )
        for text in changed:
            with self.subTest(text=text[-120:]):
                self.assertTrue(check_site.rating_script_contract_errors(text, ENDPOINT))

    def test_rating_requires_fixed_request_options_and_payload(self) -> None:
        script = self.script()
        for text in (
            script.replace("credentials: 'omit'", "credentials: 'include'", 1),
            script.replace("referrerPolicy: 'no-referrer'", "referrerPolicy: 'origin'", 1),
            script.replace("{ unit, lesson, rating }", "{ unit, lesson, rating, search }"),
            script.replace("body,", "body: document.cookie,", 1),
        ):
            self.assertTrue(check_site.rating_script_contract_errors(text, ENDPOINT))

    def test_googleusercontent_post_is_rejected(self) -> None:
        redirect_url = "https://script.googleusercontent.com/macros/echo?user_content_key=test"
        text = self.script().replace("fetch(ENDPOINT,", f"fetch('{redirect_url}',")
        self.assertIn("method: 'POST'", text)
        self.assertIn(
            "rating requests do not use the exact configured endpoint",
            check_site.javascript_privacy_contract_errors("_assets/rating.js", text, ENDPOINT),
        )

    def test_different_script_google_deployment_is_rejected_for_both_methods(self) -> None:
        other = "https://script.google.com/macros/s/different_deployment/exec"
        for target in ("fetch(ENDPOINT,", "fetch(ENDPOINT + '?unit='"):
            with self.subTest(target=target):
                text = self.script().replace(target, target.replace("ENDPOINT", f"'{other}'"))
                self.assertIn(
                    "rating requests do not use the exact configured endpoint",
                    check_site.javascript_privacy_contract_errors("_assets/rating.js", text, ENDPOINT),
                )

    def test_third_host_is_rejected_for_both_methods(self) -> None:
        for host in ("example.com", "script.googleusercontent.com.evil.example"):
            for target in ("fetch(ENDPOINT,", "fetch(ENDPOINT + '?unit='"):
                with self.subTest(host=host, target=target):
                    text = self.script().replace(
                        target, target.replace("ENDPOINT", f"'https://{host}/exec'")
                    )
                    self.assertIn(
                        "rating requests do not use the exact configured endpoint",
                        check_site.javascript_privacy_contract_errors("_assets/rating.js", text, ENDPOINT),
                    )

    def test_post_cannot_follow_a_redirect(self) -> None:
        for replacement in ("", "redirect: 'follow',", "redirect: 'manual',"):
            with self.subTest(replacement=replacement):
                text = self.script().replace("redirect: 'error',", replacement)
                self.assertIn(
                    "rating request methods, payload, or privacy options differ",
                    check_site.javascript_privacy_contract_errors("_assets/rating.js", text, ENDPOINT),
                )

    def test_rating_contract_preserves_literal_whitespace(self) -> None:
        script = self.script()
        replacements = (
            ("'?unit='", "' ?unit='"),
            ("'?unit='", "'?unit= '"),
            ("'no-referrer'", "'no- referrer'"),
            ("'no-cors'", "'no- cors'"),
            ("'omit'", "' omit'"),
            ("'reason'", "'rea son'"),
            ("padStart(2, '0')", "padStart(2, ' 0')"),
            ("`${now.getFullYear()}-", "`${now.getFullYear()}- "),
            ("`manabigrid-rating:${unit}:${lesson}`", "` manabigrid-rating:${unit}:${lesson}`"),
            ("`manabigrid-rating:${unit}:${lesson}`", "`manabigrid-rating:${unit}:${lesson} `"),
        )
        for original, changed in replacements:
            with self.subTest(original=original, changed=changed):
                self.assertIn(original, script)
                self.assertTrue(check_site.javascript_privacy_contract_errors(
                    "_assets/rating.js", script.replace(original, changed), ENDPOINT
                ))

    def test_rating_tokens_ignore_only_code_whitespace_and_comments(self) -> None:
        tokenize = check_site.javascript_contract_tokens
        self.assertEqual(tokenize("fetch ( ENDPOINT , { method : 'GET' } )"), tokenize("fetch(ENDPOINT,{method:'GET'})"))
        self.assertEqual(tokenize("const /* ignored */ body = 'a b';"), tokenize("const body='a b';"))
        self.assertNotEqual(tokenize("'a b'"), tokenize("'ab'"))
        self.assertNotEqual(tokenize('"a b"'), tokenize('"ab"'))
        self.assertNotEqual(tokenize("`a ${unit} b`"), tokenize("`a${unit}b`"))
        self.assertNotEqual(tokenize("storage Key"), tokenize("storageKey"))
        self.assertNotEqual(tokenize("'a\\' b'"), tokenize("'a\\'b'"))

    def test_local_date_return_rejects_automatic_semicolon_insertion(self) -> None:
        script = self.script()
        for replacement in (
            "return\n today;", "return\r\n today;", "return\u2028 today;",
            "return\u2029 today;", "return/*\n*/today;",
            "return // comment\n today;",
            "return\n today; // return today;",
        ):
            with self.subTest(replacement=replacement):
                self.assertTrue(check_site.javascript_privacy_contract_errors(
                    "_assets/rating.js", script.replace("return today;", replacement), ENDPOINT
                ))

    def test_rating_rejects_expanded_or_different_storage(self) -> None:
        script = self.script()
        for text in (
            script.replace("manabigrid-rating:${unit}:${lesson}", "manabigrid-other:${unit}:${lesson}"),
            script.replace("getItem(storageKey)", "getItem('other-key')"),
            script.replace("setItem(storageKey, today)", "setItem(storageKey, unit)"),
            script.replace("saveDay(today);", "saveDay(unit);"),
            script + "\nwindow.localStorage.getItem(storageKey);",
            script + "\nwindow.localStorage.setItem(storageKey, today);",
            script + "\nwindow.localStorage['setItem']('other', 'value');",
            script.replace("const today = localDay();", "const today = unit;"),
            script.replace("return today;", "return unit;"),
            script.replace("window.localStorage.setItem(storageKey, today);", "today += unit; window.localStorage.setItem(storageKey, today);"),
        ):
            self.assertTrue(check_site.rating_script_contract_errors(text, ENDPOINT))

    def test_other_network_and_tracking_bans_apply_to_rating(self) -> None:
        for code in (
            "navigator.sendBeacon(ENDPOINT, 'test');",
            "new XMLHttpRequest();", "sessionStorage.setItem('x', 'y');",
            "const analytics = true;", "const tracking = true;",
        ):
            self.assertTrue(check_site.javascript_privacy_contract_errors(
                "_assets/rating.js", self.script() + "\n" + code, ENDPOINT
            ))

    def test_both_google_hosts_remain_forbidden_to_other_scripts_and_network_apis(self) -> None:
        for url in (ENDPOINT, "https://script.googleusercontent.com/macros/echo?test"):
            for code in (
                f"fetch('{url}', {{method: 'GET'}});",
                f"fetch('{url}', {{method: 'POST'}});",
                f"navigator.sendBeacon('{url}', 'test');",
                f"const request = new XMLHttpRequest(); request.open('GET', '{url}');",
            ):
                for relative in ("_assets/site.js", "_assets/theme.js", "_assets/copied/rating.js"):
                    with self.subTest(url=url, code=code, relative=relative):
                        self.assertTrue(check_site.javascript_privacy_contract_errors(relative, code, ENDPOINT))

    def test_theme_storage_contract_is_unchanged(self) -> None:
        script = (Path(check_site.__file__).with_name("static") / "theme.js").read_text(encoding="utf-8")
        self.assertEqual(check_site.theme_storage_contract_errors(script, []), [])
        self.assertTrue(check_site.theme_storage_contract_errors(
            script.replace("manabigrid-theme", "manabigrid-rating:unit:L01"), []
        ))
        self.assertTrue(check_site.theme_storage_contract_errors(script, ["_assets/extra.js"]))

    def test_csp_allows_only_two_rating_hosts_on_rating_pages(self) -> None:
        base = (
            "default-src 'self'; script-src 'self'; connect-src 'none'; "
            "object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"
        )
        sources = "connect-src https://script.google.com https://script.googleusercontent.com"
        enabled = base.replace("connect-src 'none'", sources)
        self.assertEqual(check_site.csp_contract_errors(base), [])
        self.assertEqual(check_site.csp_contract_errors(enabled, True), [])
        self.assertTrue(check_site.csp_contract_errors(enabled, False))
        self.assertTrue(check_site.csp_contract_errors(base, True))
        for csp in (
            enabled + "; connect-src https://example.com",
            enabled.replace(sources, sources + " https://example.com"),
            enabled.replace(sources, "connect-src https://script.google.com"),
            enabled.replace(sources, "connect-src https://script.googleusercontent.com"),
            enabled.replace(sources, sources + " *"),
            enabled.replace("https://script.googleusercontent.com", "https://*.googleusercontent.com"),
            enabled.replace("script-src 'self'", "script-src *"),
        ):
            self.assertTrue(check_site.csp_contract_errors(csp, True))

    def test_host_allowlist_only_adds_rating_host_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = root / "index.html"
            def status(value: str, endpoint: str) -> str:
                return check_site.validate_link(page, "a", "href", value, root, {}, endpoint)[0]
            self.assertEqual(status(ENDPOINT, ""), "error")
            self.assertEqual(status(ENDPOINT, ENDPOINT), "external")
            self.assertEqual(status(ENDPOINT.replace("test_deployment-123", "other_deployment"), ENDPOINT), "error")
            self.assertEqual(status(ENDPOINT + "?unit=test", ENDPOINT), "error")
            self.assertEqual(status(ENDPOINT.replace("script.google.com", "script.googleusercontent.com"), ENDPOINT), "error")
            self.assertEqual(status("https://example.com", ENDPOINT), "error")

    def test_rating_identity_assets_and_hidden_state_are_exact(self) -> None:
        raw = self.markup()
        self.assertEqual(self.html_errors(raw), [])
        for altered in (
            raw.replace('data-rating-lesson="L01"', 'data-rating-lesson="L02"'),
            raw.replace('data-rating-unit="jhs-math-example"', 'data-rating-unit="other"'),
            raw.replace('data-rating-count-unit="jhs-math-example"', 'data-rating-count-unit="other"'),
            raw.replace(check_site.RATING_FEEDBACK_URL, "https://example.com"),
            raw.replace(" hidden", "", 1),
            raw.replace("rating.js", "rating.js?extra=1"),
            raw.replace("<script", "<!-- <script").replace("</script>", "</script> -->"),
            raw.replace('<script src=', '<script src="https://example.com/rating.js" src='),
            raw.replace('<section class=', '<section data-rating-unit="other" class='),
            raw.replace('class="lesson-rating"', 'class="unapproved"'),
            raw.replace('class="rating-count"', 'class="unapproved"'),
            raw.replace("<main>", "").replace("</main>", ""),
            raw.replace("</article>", '<span data-rating-lesson="L01"></span></article>'),
        ):
            self.assertTrue(self.html_errors(altered))
        self.assertTrue(self.html_errors(raw, ""))
        self.assertTrue(check_site.rating_html_contract_errors("about/index.html", raw, ENDPOINT, None, None))

    def test_rating_assets_require_executable_deferred_script_and_stylesheet(self) -> None:
        raw = self.markup()
        self.assertEqual(self.html_errors(raw.replace("<script ", '<script type="text/javascript" ')), [])
        for altered in (
            raw.replace("<script ", '<script type="text/plain" '),
            raw.replace("<script ", '<script type="module" '),
            raw.replace("<script ", '<script type="" '),
            raw.replace(" defer", ""),
            raw.replace(" defer", " defer async"),
            raw.replace(" defer", " defer nomodule"),
            raw.replace(" defer", ' defer integrity="wrong"'),
            raw.replace('rel="stylesheet"', 'rel="preload"'),
            raw.replace('rel="stylesheet"', 'rel="alternate stylesheet"'),
            raw.replace('rel="stylesheet"', ''),
            raw.replace('rel="stylesheet"', 'rel="stylesheet" disabled'),
            raw.replace('rel="stylesheet"', 'rel="stylesheet" media="print"'),
            raw.replace('rel="stylesheet"', 'rel="stylesheet" type="text/plain"'),
        ):
            with self.subTest(altered=altered):
                self.assertTrue(self.html_errors(altered))

    def test_source_enumeration_includes_special_lessons_and_natural_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "materials/jhs-math/jhs-math-example"
            package.mkdir(parents=True)
            for name in (
                "README.md", "lesson_10.md", "lesson_02.md", "lesson_01.md",
                "diagnostic.md", "student_textbook_print.md", "answer_key.md",
            ):
                (package / name).write_text("# Test\n", encoding="utf-8")
            lessons, counts = check_site.canonical_rating_pages(root)
        prefix = "content/materials/jhs-math/jhs-math-example/"
        self.assertEqual(lessons, {
            prefix + "diagnostic.html": ("jhs-math-example", "L01"),
            prefix + "lesson_01.html": ("jhs-math-example", "L02"),
            prefix + "lesson_02.html": ("jhs-math-example", "L03"),
            prefix + "lesson_10.html": ("jhs-math-example", "L04"),
            prefix + "student_textbook_print.html": ("jhs-math-example", "L05"),
        })
        self.assertEqual(len(counts), len(lessons) + 1)
        self.assertEqual(counts["units/jhs-math-example/index.html"], "jhs-math-example")


if __name__ == "__main__":
    unittest.main()
