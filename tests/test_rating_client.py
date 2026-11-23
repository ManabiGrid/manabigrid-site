from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://script.google.com/macros/s/test-deployment/exec"
UNIT = "jhs-math-1-positive-negative"
STORAGE_KEY = f"manabigrid-rating:{UNIT}:L01"

# This DOM model executes the production client without a browser or network.
# Deliberately dispatching disabled buttons also exercises its duplicate guards.
NODE_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(tag) {
    this.tagName = tag;
    this.dataset = {};
    this.className = '';
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  click() { this.listeners.click?.(); }
  focus(options) { this.focused = true; this.focusOptions = options; }
}
const descendants = (node) => [node, ...node.children.flatMap(descendants)];
const section = new Element('section');
section.hidden = true;
section.dataset.ratingUnit = input.unit;
section.dataset.ratingLesson = input.lesson || 'L01';
section.dataset.ratingFeedbackUrl = 'https://docs.google.com/forms/d/e/test/viewform';
const countNodes = Array.from({length: input.countSlots || 1}, () => {
  const node = new Element('p');
  node.hidden = true;
  node.dataset.ratingCountUnit = input.unit;
  return node;
});
const storage = new Map();
const storageKey = `manabigrid-rating:${input.unit}:${input.lesson || 'L01'}`;
if (input.savedDay) storage.set(storageKey, input.savedDay);
let storageReads = 0;
const storageWrites = [];
const listeners = {};
const document = {
  readyState: input.loading ? 'loading' : 'complete',
  createElement: (tag) => new Element(tag),
  addEventListener: (name, fn) => { listeners[name] = fn; },
  querySelectorAll: (selector) => {
    if (selector === '[data-rating-unit][data-rating-lesson]') return input.noSection ? [] : [section];
    if (selector === '[data-rating-count-unit]') return countNodes;
    throw new Error(`Unexpected selector: ${selector}`);
  },
};
const calls = [];
const fetch = (url, options) => {
  calls.push({
    url,
    method: options.method,
    credentials: options.credentials,
    referrerPolicy: options.referrerPolicy,
    mode: options.mode || null,
    redirect: options.redirect || null,
    body: options.body ? Object.fromEntries(options.body.entries()) : null,
    optionKeys: Object.keys(options).sort(),
  });
  if (options.method === 'POST') {
    return input.postFailure ? Promise.reject(new Error('offline')) : Promise.resolve({});
  }
  if (input.getFailure) return Promise.reject(new Error('offline'));
  return Promise.resolve({
    ok: input.getOk !== false,
    json: () => input.invalidJson
      ? Promise.reject(new Error('invalid JSON'))
      : Promise.resolve(input.payload === undefined ? {unit: input.unit, got: null} : input.payload),
  });
};
class FixedDate extends Date {
  constructor() { super('2026-10-23T12:00:00'); }
}
const context = {
  document,
  window: {localStorage: {
    getItem: (key) => {
      storageReads += 1;
      if (input.storageFailure) throw new Error('storage unavailable');
      return storage.get(key) ?? null;
    },
    setItem: (key, value) => {
      if (input.storageFailure) throw new Error('storage unavailable');
      storageWrites.push({key, value});
      storage.set(key, value);
    },
  }},
  fetch,
  URLSearchParams,
  Date: FixedDate,
};
const source = fs.readFileSync('static/rating.js', 'utf8')
  .replace('"__RATING_ENDPOINT__"', JSON.stringify(input.endpoint));
vm.runInNewContext(source, context);
if (input.loading && input.domLoad !== false) listeners.DOMContentLoaded?.();
const byClass = (name) => descendants(section).filter((node) => node.className === name);
for (const action of input.actions || []) {
  const nodes = byClass(action.type === 'rating' ? 'rating-option' : 'rating-reason');
  const key = action.type === 'rating' ? 'ratingValue' : 'ratingReason';
  const node = nodes.find((item) => item.dataset[key] === action.value);
  if (!node) throw new Error(`Missing control: ${JSON.stringify(action)}`);
  node.click();
}
const postCountBeforeMicrotasks = calls.filter((call) => call.method === 'POST').length;
setImmediate(() => {
  const feedback = byClass('rating-feedback')[0];
  const status = byClass('rating-status')[0];
  process.stdout.write(JSON.stringify({
    hidden: section.hidden,
    optionsHidden: byClass('rating-options')[0]?.hidden ?? null,
    ratings: byClass('rating-option').map((node) => ({
      value: node.dataset.ratingValue,
      text: node.textContent,
      disabled: node.disabled,
      pressed: node.attributes['aria-pressed'] || null,
    })),
    reasonsHidden: byClass('rating-reasons')[0]?.hidden ?? null,
    reasons: byClass('rating-reason').map((node) => ({
      value: node.dataset.ratingReason,
      text: node.textContent,
      disabled: node.disabled,
      pressed: node.attributes['aria-pressed'] || null,
    })),
    status: byClass('rating-status')[0]?.textContent || '',
    statusFocused: status?.focused || false,
    statusLive: status?.attributes['aria-live'] || null,
    statusTabIndex: status?.tabIndex ?? null,
    statusFocusOptions: status?.focusOptions || null,
    actionOrder: byClass('rating-actions')[0]?.children.map((node) => node.className) || [],
    feedback: feedback ? {
      text: feedback.textContent, href: feedback.href,
      rel: feedback.rel, referrerPolicy: feedback.referrerPolicy,
    } : null,
    counts: countNodes.map((node) => ({hidden: node.hidden, text: node.textContent})),
    calls,
    postCountBeforeMicrotasks,
    storage: Object.fromEntries(storage),
    storageReads,
    storageWrites,
  }));
});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the rating client")
class RatingClientTests(unittest.TestCase):
    def run_client(self, **options: object) -> dict:
        data = {"unit": UNIT, "endpoint": ENDPOINT, **options}
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_HARNESS],
            cwd=ROOT,
            input=json.dumps(data),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    @staticmethod
    def posts(result: dict) -> list[dict]:
        return [call for call in result["calls"] if call["method"] == "POST"]

    def test_unconfigured_or_unapproved_endpoint_has_no_effect(self) -> None:
        for endpoint in ("", "__RATING_ENDPOINT__", "https://example.com/exec"):
            with self.subTest(endpoint=endpoint):
                result = self.run_client(endpoint=endpoint)
                self.assertTrue(result["hidden"])
                self.assertEqual(result["ratings"], [])
                self.assertEqual(result["calls"], [])
                self.assertEqual(result["storageReads"], 0)

    def test_initial_controls_and_forms_link_do_not_submit_a_rating(self) -> None:
        result = self.run_client()
        self.assertFalse(result["hidden"])
        self.assertEqual(
            [button["text"] for button in result["ratings"]],
            ["わかった", "すこしむずかしい", "わからなかった"],
        )
        self.assertTrue(result["reasonsHidden"])
        self.assertFalse(result["optionsHidden"])
        self.assertEqual(self.posts(result), [])
        self.assertEqual(result["feedback"]["text"], "ひとこと書く")
        self.assertEqual(result["feedback"]["rel"], "noopener noreferrer")
        self.assertEqual(result["feedback"]["referrerPolicy"], "no-referrer")
        self.assertFalse(result["statusFocused"])

    def test_one_click_submits_once_and_stores_only_the_date(self) -> None:
        result = self.run_client(actions=[
            {"type": "rating", "value": "got"},
            {"type": "rating", "value": "hard"},
        ])
        posts = self.posts(result)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["url"], ENDPOINT)
        self.assertEqual(posts[0]["body"], {"unit": UNIT, "lesson": "L01", "rating": "got"})
        self.assertEqual(posts[0]["mode"], "no-cors")
        self.assertEqual(posts[0]["credentials"], "omit")
        self.assertEqual(posts[0]["referrerPolicy"], "no-referrer")
        self.assertEqual(posts[0]["redirect"], "error")
        self.assertEqual(posts[0]["optionKeys"], ["body", "credentials", "method", "mode", "redirect", "referrerPolicy"])
        self.assertEqual(result["storage"], {STORAGE_KEY: "2026-10-23"})
        self.assertEqual(result["storageWrites"], [{"key": STORAGE_KEY, "value": "2026-10-23"}])
        self.assertEqual(result["status"], "ありがとう")
        self.assertTrue(result["optionsHidden"])
        self.assertTrue(result["statusFocused"])
        self.assertEqual(result["statusLive"], "off")
        self.assertEqual(result["statusTabIndex"], -1)
        self.assertEqual(result["statusFocusOptions"], {"preventScroll": True})
        self.assertEqual(result["actionOrder"], ["rating-options", "rating-status", "rating-feedback"])
        self.assertTrue(all(button["disabled"] for button in result["ratings"]))
        self.assertTrue(result["reasonsHidden"])

    def test_hard_and_lost_offer_one_optional_reason_after_the_initial_post(self) -> None:
        for rating in ("hard", "lost"):
            with self.subTest(rating=rating):
                result = self.run_client(actions=[
                    {"type": "rating", "value": rating},
                    {"type": "reason", "value": "wording"},
                    {"type": "reason", "value": "error"},
                ])
                self.assertFalse(result["reasonsHidden"])
                self.assertEqual([button["text"] for button in result["reasons"]], [
                    "例が足りない", "ことばが難しい", "話が飛ぶ", "まちがいがありそう",
                ])
                self.assertEqual(result["postCountBeforeMicrotasks"], 1)
                posts = self.posts(result)
                self.assertEqual(len(posts), 2)
                self.assertEqual(posts[0]["body"], {"unit": UNIT, "lesson": "L01", "rating": rating})
                self.assertEqual(posts[1]["body"], {
                    "unit": UNIT, "lesson": "L01", "rating": rating, "reason": "wording",
                })
                self.assertTrue(all(button["disabled"] for button in result["reasons"]))
                self.assertEqual(len(result["storageWrites"]), 1)
                self.assertTrue(result["statusFocused"])

    def test_same_day_is_disabled_and_previous_day_can_answer(self) -> None:
        today = self.run_client(savedDay="2026-10-23", actions=[{"type": "rating", "value": "got"}])
        self.assertEqual(self.posts(today), [])
        self.assertEqual(today["status"], "ありがとう")
        self.assertTrue(today["optionsHidden"])
        self.assertFalse(today["statusFocused"])
        self.assertTrue(all(button["disabled"] for button in today["ratings"]))
        yesterday = self.run_client(savedDay="2026-10-22", actions=[{"type": "rating", "value": "got"}])
        self.assertEqual(len(self.posts(yesterday)), 1)

    def test_storage_unavailable_and_network_failure_remain_quiet(self) -> None:
        result = self.run_client(storageFailure=True, postFailure=True, getFailure=True, actions=[
            {"type": "rating", "value": "lost"},
            {"type": "reason", "value": "examples"},
            {"type": "rating", "value": "got"},
        ])
        self.assertEqual(len(self.posts(result)), 2)
        self.assertEqual(result["status"], "ありがとう")
        self.assertEqual(result["storage"], {})
        self.assertTrue(result["counts"][0]["hidden"])

    def test_counts_show_only_valid_unit_totals_above_ten(self) -> None:
        hidden_payloads = [
            {"unit": UNIT, "got": value} for value in (None, 0, 10, -1, "11", True, 11.5)
        ] + [{"unit": "different-unit", "got": 11}, {"got": 11}, None]
        for payload in hidden_payloads:
            with self.subTest(payload=payload):
                result = self.run_client(payload=payload)
                self.assertTrue(result["counts"][0]["hidden"])
                self.assertEqual(result["counts"][0]["text"], "")
        result = self.run_client(payload={"unit": UNIT, "got": 11}, countSlots=2, noSection=True)
        self.assertEqual(result["counts"], [
            {"hidden": False, "text": "この単元の「わかった」11回"},
            {"hidden": False, "text": "この単元の「わかった」11回"},
        ])
        self.assertEqual(len(result["calls"]), 1)
        self.assertEqual(result["calls"][0]["url"], f"{ENDPOINT}?unit={UNIT}")
        self.assertEqual(result["calls"][0]["method"], "GET")
        self.assertIsNone(result["calls"][0]["redirect"])  # Fetch defaults to following GET redirects.
        self.assertEqual(result["calls"][0]["credentials"], "omit")
        self.assertEqual(result["calls"][0]["referrerPolicy"], "no-referrer")
        self.assertEqual(result["storageReads"], 0)

    def test_failed_or_invalid_get_does_not_reveal_counts(self) -> None:
        for options in ({"getFailure": True}, {"getOk": False}, {"invalidJson": True}):
            with self.subTest(options=options):
                result = self.run_client(**options)
                self.assertTrue(result["counts"][0]["hidden"])

    def test_initialization_waits_for_the_document(self) -> None:
        waiting = self.run_client(loading=True, domLoad=False)
        self.assertTrue(waiting["hidden"])
        self.assertEqual(waiting["calls"], [])
        loaded = self.run_client(loading=True)
        self.assertFalse(loaded["hidden"])

    def test_invalid_unit_or_lesson_is_not_activated(self) -> None:
        for options in (
            {"unit": "a"}, {"unit": "has_under_score"}, {"unit": "a" * 81},
            {"unit": "Uppercase"}, {"lesson": "L1"}, {"lesson": "L123"},
        ):
            with self.subTest(options=options):
                result = self.run_client(**options)
                self.assertTrue(result["hidden"])
                self.assertEqual(result["ratings"], [])
                self.assertEqual(result["storageReads"], 0)


if __name__ == "__main__":
    unittest.main()
