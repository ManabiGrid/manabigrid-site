/**
 * ManabiGrid anonymous lesson ratings. Paste into the owner's Apps Script project.
 * No deployment is performed by this repository. See README.md before use.
 */
var RATING_CONFIG = Object.freeze({
  spreadsheetId: 'PASTE_SPREADSHEET_ID_HERE',
  sheetName: 'ratings',
  maxBodyLength: 512,
  windowMs: 60000,
  globalPerMinute: 120,
  lessonPerMinute: 20,
  lockTimeoutMs: 2000,
  cachePrefix: 'manabigrid-rating-v1:'
});

var RATING_HEADERS = Object.freeze(['timestamp', 'unit', 'lesson', 'rating', 'reason']);
var RATING_REASONS = Object.freeze(['examples', 'wording', 'jump', 'error']);

/** Owner-only setup: run manually once, before deploying the Web App. */
function prepareRatingSheet() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(RATING_CONFIG.lockTimeoutMs)) {
    throw new Error('rating_setup_busy');
  }
  try {
    var spreadsheet = ratingSpreadsheet_();
    var sheet = spreadsheet.getSheetByName(RATING_CONFIG.sheetName);
    if (!sheet) sheet = spreadsheet.insertSheet(RATING_CONFIG.sheetName);
    if (sheet.getLastRow() === 0) {
      sheet.getRange(1, 1, 1, RATING_HEADERS.length).setValues([RATING_HEADERS.slice()]);
    }
    ratingCheckHeaders_(sheet);
    sheet.setFrozenRows(1);
  } finally {
    lock.releaseLock();
  }
}

/** Accept URLSearchParams form bodies only; query parameters are forbidden. */
function doPost(e) {
  var fields;
  try {
    if (!e || (e.queryString !== undefined && e.queryString !== null && e.queryString !== '') ||
        !e.postData || typeof e.postData.type !== 'string' ||
        !/^application\/x-www-form-urlencoded(?:\s*;\s*charset=utf-8)?$/i.test(e.postData.type)) {
      return ratingJson_({ok: false, error: 'invalid_request'});
    }
    fields = ratingParseForm_(e.postData.contents, ['unit', 'lesson', 'rating', 'reason']);
    if (!fields || !ratingValidUnit_(fields.unit) || !ratingValidLesson_(fields.lesson) ||
        ['got', 'hard', 'lost'].indexOf(fields.rating) === -1 ||
        (Object.prototype.hasOwnProperty.call(fields, 'reason') &&
          (fields.rating === 'got' || RATING_REASONS.indexOf(fields.reason) === -1))) {
      return ratingJson_({ok: false, error: 'invalid_request'});
    }
  } catch (_) {
    return ratingJson_({ok: false, error: 'invalid_request'});
  }

  return ratingWithLock_(function () {
    if (!ratingReserveRate_(fields.unit, fields.lesson)) {
      return {ok: false, error: 'rate_limited'};
    }
    var sheet = ratingSheet_();
    var nextRow = sheet.getLastRow() + 1;
    // Preserve numeric-looking unit IDs exactly; only these five cells are stored.
    sheet.getRange(nextRow, 2, 1, 4).setNumberFormat('@');
    sheet.getRange(nextRow, 1, 1, 5).setValues([
      [new Date(), fields.unit, fields.lesson, fields.rating, fields.reason || '']
    ]);
    SpreadsheetApp.flush();
    return {ok: true};
  });
}

/** Return the unit total only when more than ten got responses exist. */
function doGet(e) {
  var fields;
  try {
    fields = ratingParseForm_(e && e.queryString, ['unit']);
    if (!fields || !ratingValidUnit_(fields.unit)) {
      return ratingJson_({ok: false, error: 'invalid_request'});
    }
  } catch (_) {
    return ratingJson_({ok: false, error: 'invalid_request'});
  }

  return ratingWithLock_(function () {
    if (!ratingReserveRate_(null, null)) return {ok: false, error: 'rate_limited'};
    var sheet = ratingSheet_();
    var lastRow = sheet.getLastRow();
    var got = 0;
    if (lastRow > 1) {
      var rows = sheet.getRange(2, 2, lastRow - 1, 4).getValues();
      rows.forEach(function (row) {
        if (row[0] === fields.unit && ratingValidLesson_(row[1]) &&
            row[2] === 'got' && row[3] === '') got += 1;
      });
    }
    return {unit: fields.unit, got: got > 10 ? got : null};
  });
}

/** Parse the URLSearchParams wire format without trusting flattened e.parameter. */
function ratingParseForm_(raw, allowedKeys) {
  if (typeof raw !== 'string' || raw.length === 0 || raw.length > RATING_CONFIG.maxBodyLength ||
      !/^[\x20-\x7e]+$/.test(raw)) return null;
  var fields = Object.create(null);
  var parts = raw.split('&');
  for (var i = 0; i < parts.length; i += 1) {
    var separator = parts[i].indexOf('=');
    if (separator <= 0) return null;
    var key = decodeURIComponent(parts[i].slice(0, separator).replace(/\+/g, ' '));
    var value = decodeURIComponent(parts[i].slice(separator + 1).replace(/\+/g, ' '));
    if (allowedKeys.indexOf(key) === -1 || Object.prototype.hasOwnProperty.call(fields, key)) return null;
    fields[key] = value;
  }
  return fields;
}

function ratingValidUnit_(unit) {
  if (typeof unit !== 'string') return false;
  var match = /^[a-z0-9-]{3,80}$/.exec(unit);
  // JavaScript's $ can also match before a final newline. Require the whole value.
  return match !== null && match[0] === unit;
}

function ratingValidLesson_(lesson) {
  return typeof lesson === 'string' && lesson.length === 3 && /^L\d{2}$/.test(lesson);
}

/** Sliding 60-second admission limits. Caller must hold the script lock. */
function ratingReserveRate_(unit, lesson) {
  var cache = CacheService.getScriptCache();
  var now = Date.now();
  var keys = [RATING_CONFIG.cachePrefix + 'global'];
  var limits = [RATING_CONFIG.globalPerMinute];
  if (unit !== null && lesson !== null) {
    keys.push(RATING_CONFIG.cachePrefix + 'lesson:' + unit + ':' + lesson);
    limits.push(RATING_CONFIG.lessonPerMinute);
  }
  var cached = cache.getAll(keys);
  var updates = {};
  for (var i = 0; i < keys.length; i += 1) {
    var timestamps = cached[keys[i]] ? JSON.parse(cached[keys[i]]) : [];
    if (!Array.isArray(timestamps) || timestamps.some(function (stamp) {
      return typeof stamp !== 'number' || !isFinite(stamp) || stamp > now;
    })) throw new Error('rating_cache_invalid');
    timestamps = timestamps.filter(function (stamp) { return stamp > now - RATING_CONFIG.windowMs; });
    if (timestamps.length >= limits[i]) return false;
    timestamps.push(now);
    updates[keys[i]] = JSON.stringify(timestamps);
  }
  cache.putAll(updates, Math.ceil(RATING_CONFIG.windowMs / 1000));
  return true;
}

function ratingSpreadsheet_() {
  if (!/^[a-zA-Z0-9_-]{20,}$/.test(RATING_CONFIG.spreadsheetId) ||
      RATING_CONFIG.spreadsheetId === 'PASTE_SPREADSHEET_ID_HERE') {
    throw new Error('rating_not_configured');
  }
  return SpreadsheetApp.openById(RATING_CONFIG.spreadsheetId);
}

function ratingSheet_() {
  var sheet = ratingSpreadsheet_().getSheetByName(RATING_CONFIG.sheetName);
  if (!sheet) throw new Error('rating_not_configured');
  ratingCheckHeaders_(sheet);
  return sheet;
}

function ratingCheckHeaders_(sheet) {
  if (sheet.getLastRow() < 1 || sheet.getLastColumn() !== RATING_HEADERS.length) {
    throw new Error('rating_sheet_invalid');
  }
  var headers = sheet.getRange(1, 1, 1, RATING_HEADERS.length).getValues()[0];
  if (headers.some(function (value, index) { return value !== RATING_HEADERS[index]; })) {
    throw new Error('rating_sheet_invalid');
  }
}

function ratingWithLock_(operation) {
  var lock;
  var locked = false;
  try {
    lock = LockService.getScriptLock();
    locked = lock.tryLock(RATING_CONFIG.lockTimeoutMs);
    if (!locked) return ratingJson_({ok: false, error: 'busy'});
    return ratingJson_(operation());
  } catch (_) {
    // Do not expose spreadsheet IDs, data, or exception details to anonymous users.
    return ratingJson_({ok: false, error: 'service_unavailable'});
  } finally {
    if (locked) lock.releaseLock();
  }
}

function ratingJson_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
