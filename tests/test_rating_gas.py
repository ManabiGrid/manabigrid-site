"""Offline Apps Script contract tests; no Google service or network is contacted."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const testCase = process.argv[2];
const headers = ['timestamp', 'unit', 'lesson', 'rating', 'reason'];

function create() {
  const state = {
    now: 1700000000000, rows: [headers.slice()], cache: new Map(), hasSheet: true,
    lockAvailable: true, releases: 0, sheetReads: 0, sheetWrites: 0,
    cacheError: false, readError: false, writeError: false, flushError: false
  };
  class MockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [state.now])); }
    static now() { return state.now; }
  }
  const sheet = {
    getLastRow() { return state.rows.length; },
    getLastColumn() { return Math.max(0, ...state.rows.map(row => row.length)); },
    setFrozenRows() {},
    getRange(row, column, rowCount, columnCount) {
      return {
        setNumberFormat() { return this; },
        getValues() {
          state.sheetReads += 1;
          if (state.readError) throw Error('private read failure');
          return Array.from({length: rowCount}, (_, offset) =>
            Array.from({length: columnCount}, (_, col) =>
              (state.rows[row + offset - 1] || [])[column + col - 1] ?? ''));
        },
        setValues(values) {
          if (state.writeError) throw Error('private write failure');
          state.sheetWrites += 1;
          values.forEach((valuesRow, offset) => {
            const target = state.rows[row + offset - 1] || [];
            valuesRow.forEach((value, col) => { target[column + col - 1] = value; });
            state.rows[row + offset - 1] = target;
          });
        }
      };
    }
  };
  const context = vm.createContext({
    Date: MockDate,
    ContentService: {
      MimeType: {JSON: 'application/json'},
      createTextOutput(text) { return {text, setMimeType(type) { this.type = type; return this; }}; }
    },
    LockService: {
      getScriptLock() { return {
        tryLock() { return state.lockAvailable; },
        releaseLock() { state.releases += 1; }
      }; }
    },
    CacheService: {
      getScriptCache() { return {
        getAll(keys) {
          if (state.cacheError) throw Error('private cache failure');
          const result = {};
          keys.forEach(key => {
            const cached = state.cache.get(key);
            if (cached && cached.expires > state.now) result[key] = cached.value;
          });
          return result;
        },
        putAll(values, lifetime) {
          if (state.cacheError) throw Error('private cache failure');
          Object.entries(values).forEach(([key, value]) => {
            state.cache.set(key, {value, expires: state.now + lifetime * 1000});
          });
        }
      }; }
    },
    SpreadsheetApp: {
      openById() { return {
        getSheetByName() { return state.hasSheet ? sheet : null; },
        insertSheet() { state.hasSheet = true; return sheet; }
      }; },
      flush() { if (state.flushError) throw Error('private flush failure'); }
    }
  });
  vm.runInContext(source, context);
  vm.runInContext("RATING_CONFIG = Object.freeze(Object.assign({}, RATING_CONFIG, {spreadsheetId: 'offline_spreadsheet_fixture'}));", context);
  function decoded(output) {
    assert.equal(output.type, 'application/json');
    return JSON.parse(output.text);
  }
  const post = (body = 'unit=unit-1&lesson=L01&rating=got', type = 'application/x-www-form-urlencoded;charset=UTF-8', queryString = null) =>
    decoded(context.doPost({queryString, postData: {type, contents: body}}));
  const get = (query = 'unit=unit-1') => decoded(context.doGet({queryString: query}));
  return {state, context, post, get};
}

if (testCase === 'valid') {
  const {state, post} = create();
  assert.deepEqual(post(), {ok: true});
  for (const rating of ['hard', 'lost']) {
    assert.deepEqual(post('unit=unit-1&lesson=L02&rating=' + rating), {ok: true});
    for (const reason of ['examples', 'wording', 'jump', 'error']) {
      assert.deepEqual(post('unit=unit-1&lesson=L02&rating=' + rating + '&reason=' + reason), {ok: true});
    }
  }
  assert.equal(state.rows.length, 12);
  assert.equal(state.rows[1].length, 5);
  assert.equal(state.rows[1][0].getTime(), state.now);
  assert.deepEqual(state.rows[1].slice(1), ['unit-1', 'L01', 'got', '']);
  for (const unit of ['001', '-12', 'a'.repeat(80)]) {
    assert.deepEqual(post('unit=' + unit + '&lesson=L00&rating=got'), {ok: true});
    assert.equal(state.rows.at(-1)[1], unit);
  }
}
if (testCase === 'reject') {
  const {state, post, context} = create();
  const valid = 'unit=unit-1&lesson=L01&rating=got';
  const invalid = [
    '', 'unit=unit-1', valid + '&unit=other', valid + '&%75nit=other',
    valid + '&comment=hello', valid + '&reason=examples',
    valid.replace('got', 'hard') + '&reason=',
    valid.replace('got', 'hard') + '&reason=free-text',
    valid.replace('unit-1', 'ab'), valid.replace('unit-1', 'A-B'),
    valid.replace('unit-1', 'a'.repeat(81)), valid.replace('unit-1', '%ZZ'),
    valid.replace('unit-1', '%E3%81'), valid.replace('unit-1', 'a+b'),
    valid.replace('unit-1', 'unit-1%0A'), valid.replace('unit-1', 'unit-1%0D'),
    valid.replace('unit-1', 'unit-1%E2%80%A8'), valid.replace('unit-1', 'unit-1%E2%80%A9'),
    valid.replace('unit-1', 'unit%26lesson%3DL02'),
    valid.replace('L01', 'L1'), valid.replace('L01', 'L001'),
    valid.replace('L01', 'L01%0A'), valid.replace('L01', 'L01%E2%80%A8'),
    valid.replace('L01', 'l01'), valid.replace('got', 'GOT'),
    valid + '&', '&' + valid, valid + '&rating=hard',
    JSON.stringify({unit: 'unit-1', lesson: 'L01', rating: 'got'}),
    valid + '&__proto__=value', valid + '&callback=alert',
    valid.replace('unit-1', '日本語'), 'x'.repeat(513)
  ];
  invalid.forEach(body => assert.deepEqual(post(body), {ok: false, error: 'invalid_request'}, body));
  for (const type of ['application/json', 'text/plain', 'multipart/form-data', '', 'application/x-www-form-urlencoded; boundary=x']) {
    assert.deepEqual(post(valid, type), {ok: false, error: 'invalid_request'});
  }
  assert.deepEqual(post(valid, 'application/x-www-form-urlencoded', 'unit=unit-1'), {ok: false, error: 'invalid_request'});
  assert.equal(JSON.parse(context.doPost(null).text).error, 'invalid_request');
  assert.equal(state.rows.length, 1);
  assert.equal(state.sheetReads, 0);
  assert.equal(state.cache.size, 0);
}
if (testCase === 'threshold') {
  const {state, get} = create();
  assert.deepEqual(get(), {unit: 'unit-1', got: null});
  for (let i = 0; i < 10; i += 1) state.rows.push([new Date(), 'unit-1', 'L01', 'got', '']);
  state.rows.push([new Date(), 'other-unit', 'L01', 'got', '']);
  state.rows.push([new Date(), 'unit-1', 'L01', 'hard', '']);
  state.rows.push([new Date(), 'unit-1', 'L01', 'lost', 'examples']);
  state.rows.push([new Date(), 'unit-1', 'L01', 'got', 'examples']);
  assert.deepEqual(get(), {unit: 'unit-1', got: null});
  state.rows.push([new Date(), 'unit-1', 'L02', 'got', '']);
  assert.deepEqual(get(), {unit: 'unit-1', got: 11});
  const serialized = JSON.stringify(get());
  assert.equal(serialized.includes('reason'), false);
  assert.equal(serialized.includes('timestamp'), false);
  assert.equal(serialized.includes('L01'), false);
}
if (testCase === 'get-reject') {
  const {state, get, context} = create();
  for (const query of ['', 'unit=unit-1&unit=unit-1', 'unit=unit-1&%75nit=unit-2', 'unit=unit-1&lesson=L01', 'unit=unit-1&callback=x', 'unit=ab', 'unit=%ZZ', 'unit=unit-1&']) {
    assert.deepEqual(get(query), {ok: false, error: 'invalid_request'});
  }
  assert.equal(JSON.parse(context.doGet(null).text).error, 'invalid_request');
  assert.equal(state.sheetReads, 0);
}
if (testCase === 'lesson-limit') {
  const {state, post} = create();
  for (let i = 0; i < 20; i += 1) assert.equal(post().ok, true);
  assert.deepEqual(post(), {ok: false, error: 'rate_limited'});
  assert.equal(state.rows.length, 21);
  assert.equal(post('unit=unit-1&lesson=L02&rating=got').ok, true);
  state.now += 59999;
  assert.deepEqual(post(), {ok: false, error: 'rate_limited'});
  state.now += 1;
  assert.equal(post().ok, true);
  assert.equal(state.rows.length, 23);
}
if (testCase === 'global-limit') {
  const {state, post, get} = create();
  for (let i = 0; i < 120; i += 1) assert.equal(post('unit=unit-' + i + '&lesson=L01&rating=got').ok, true);
  assert.deepEqual(post('unit=unit-999&lesson=L01&rating=got'), {ok: false, error: 'rate_limited'});
  assert.deepEqual(get(), {ok: false, error: 'rate_limited'});
  assert.equal(state.rows.length, 121);
  state.now += 60000;
  assert.equal(post().ok, true);
}
if (testCase === 'read-limit') {
  const {state, get, post} = create();
  for (let i = 0; i < 120; i += 1) assert.equal(get().got, null);
  const reads = state.sheetReads;
  assert.deepEqual(get(), {ok: false, error: 'rate_limited'});
  assert.deepEqual(post(), {ok: false, error: 'rate_limited'});
  assert.equal(state.sheetReads, reads);
  assert.equal(state.rows.length, 1);
}
if (testCase === 'lock-failure') {
  const {state, post, get} = create();
  state.lockAvailable = false;
  assert.deepEqual(post(), {ok: false, error: 'busy'});
  assert.deepEqual(get(), {ok: false, error: 'busy'});
  assert.equal(state.sheetReads, 0);
  assert.equal(state.sheetWrites, 0);
  assert.equal(state.cache.size, 0);
  assert.equal(state.releases, 0);
}
if (testCase === 'service-failure') {
  for (const failure of ['cacheError', 'readError', 'writeError']) {
    const {state, post} = create();
    state[failure] = true;
    assert.deepEqual(post(), {ok: false, error: 'service_unavailable'});
    assert.equal(state.rows.length, 1);
    assert.equal(state.releases, 1);
  }
  const {state, post} = create();
  state.flushError = true;
  assert.deepEqual(post(), {ok: false, error: 'service_unavailable'});
  // A failed flush can have an uncertain write outcome; callers must not auto retry.
  assert.equal(state.rows.length, 2);
}
if (testCase === 'sheet-contract') {
  for (const corruption of ['missing', 'header', 'extra-column', 'cache']) {
    const {state, post} = create();
    if (corruption === 'missing') state.hasSheet = false;
    if (corruption === 'header') state.rows[0][0] = 'changed';
    if (corruption === 'extra-column') state.rows[0].push('private');
    if (corruption === 'cache') state.cache.set('manabigrid-rating-v1:global', {value: '{', expires: state.now + 60000});
    assert.deepEqual(post(), {ok: false, error: 'service_unavailable'});
    assert.equal(state.rows.length, 1);
  }
}
if (testCase === 'setup') {
  const {state, context} = create();
  state.rows = [];
  state.hasSheet = false;
  context.prepareRatingSheet();
  assert.equal(state.hasSheet, true);
  assert.deepEqual(state.rows[0], headers);
  assert.equal(state.rows.length, 1);
  context.prepareRatingSheet();
  assert.equal(state.rows.length, 1);
}
"""


@unittest.skipUnless(NODE, "Node.js is required for the offline Apps Script behavior tests")
class RatingGasTests(unittest.TestCase):
    def run_case(self, name: str) -> None:
        result = subprocess.run(
            [NODE, "-e", HARNESS, str(ROOT / "gas/rating/Code.gs"), name],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_valid_votes_store_only_the_five_fields(self) -> None:
        self.run_case("valid")

    def test_post_rejects_duplicates_unknown_keys_and_free_text(self) -> None:
        self.run_case("reject")

    def test_unit_total_is_hidden_until_the_eleventh_got_vote(self) -> None:
        self.run_case("threshold")

    def test_get_rejects_extra_and_duplicate_parameters(self) -> None:
        self.run_case("get-reject")

    def test_lesson_rate_limit_blocks_writes_for_sixty_seconds(self) -> None:
        self.run_case("lesson-limit")

    def test_global_rate_limit_blocks_other_units_and_get(self) -> None:
        self.run_case("global-limit")

    def test_public_reads_share_the_global_limit(self) -> None:
        self.run_case("read-limit")

    def test_lock_contention_never_writes_or_consumes_cache(self) -> None:
        self.run_case("lock-failure")

    def test_service_failures_are_private_and_fail_closed(self) -> None:
        self.run_case("service-failure")

    def test_missing_sheet_header_drift_and_bad_cache_never_append(self) -> None:
        self.run_case("sheet-contract")

    def test_manual_setup_keeps_existing_rows(self) -> None:
        self.run_case("setup")


if __name__ == "__main__":
    unittest.main()
