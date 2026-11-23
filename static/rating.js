(function () {
  'use strict';

  const ENDPOINT = "__RATING_ENDPOINT__";
  const RATINGS = [
    ['got', 'わかった'],
    ['hard', 'すこしむずかしい'],
    ['lost', 'わからなかった'],
  ];
  const REASONS = [
    ['examples', '例が足りない'],
    ['wording', 'ことばが難しい'],
    ['jump', '話が飛ぶ'],
    ['error', 'まちがいがありそう'],
  ];
  const validUnit = (unit) => /^[a-z0-9-]{3,80}$/.test(unit || '');

  if (!/^https:\/\/script\.google\.com\/macros\/s\/[A-Za-z0-9_-]+\/exec$/.test(ENDPOINT)) {
    return;
  }

  const localDay = () => {
    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    return today;
  };

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    node.className = className;
    if (text) node.textContent = text;
    return node;
  };

  const postRating = (unit, lesson, rating, reason) => {
    const body = new URLSearchParams({ unit, lesson, rating });
    if (reason) body.set('reason', reason);
    return fetch(ENDPOINT, {
      method: 'POST',
      mode: 'no-cors',
      // Only the count GET may follow Google's ContentService redirect.
      redirect: 'error',
      body,
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
    }).catch(() => {});
  };

  const createRating = (section) => {
    const unit = section.dataset.ratingUnit;
    const lesson = section.dataset.ratingLesson;
    if (!validUnit(unit) || !/^L\d{2}$/.test(lesson || '')) return;

    const storageKey = `manabigrid-rating:${unit}:${lesson}`;
    const savedDay = () => {
      try {
        return window.localStorage.getItem(storageKey);
      } catch (_error) {
        return null;
      }
    };
    const saveDay = (today) => {
      try {
        window.localStorage.setItem(storageKey, today);
      } catch (_error) {
        // The current page still prevents another answer when storage is unavailable.
      }
    };

    const title = element('h2', 'rating-title', 'このレッスンはどうでしたか');
    const actions = element('div', 'rating-actions');
    const options = element('div', 'rating-options');
    options.setAttribute('role', 'group');
    options.setAttribute('aria-label', 'このレッスンはどうでしたか');
    const status = element('p', 'rating-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.tabIndex = -1;
    const reasons = element('div', 'rating-reasons');
    reasons.hidden = true;
    const reasonPrompt = element('p', 'rating-reason-prompt', 'よければ、理由をひとつ教えてください。');
    const reasonOptions = element('div', 'rating-reason-options');
    reasonOptions.setAttribute('role', 'group');
    reasonOptions.setAttribute('aria-label', 'むずかしかった理由');
    reasons.append(reasonPrompt, reasonOptions);

    let rated = false;
    let reasonSent = false;
    let selectedRating = null;
    let initialPost = Promise.resolve();
    const ratingButtons = [];
    const reasonButtons = [];
    const thank = (moveFocus = false) => {
      rated = true;
      ratingButtons.forEach((button) => { button.disabled = true; });
      options.hidden = true;
      // Focus supplies the announcement after a click; do not also announce it live.
      if (moveFocus) status.setAttribute('aria-live', 'off');
      status.textContent = 'ありがとう';
      if (moveFocus) status.focus({ preventScroll: true });
    };

    RATINGS.forEach(([value, label]) => {
      const button = element('button', 'rating-option', label);
      button.type = 'button';
      button.dataset.ratingValue = value;
      button.addEventListener('click', () => {
        const today = localDay();
        if (rated) return;
        if (savedDay() === today) {
          thank(true);
          return;
        }
        selectedRating = value;
        saveDay(today);
        button.setAttribute('aria-pressed', 'true');
        thank(true);
        initialPost = postRating(unit, lesson, value);
        reasons.hidden = value === 'got';
      });
      ratingButtons.push(button);
      options.append(button);
    });

    REASONS.forEach(([value, label]) => {
      const button = element('button', 'rating-reason', label);
      button.type = 'button';
      button.dataset.ratingReason = value;
      button.addEventListener('click', () => {
        if (reasonSent || (selectedRating !== 'hard' && selectedRating !== 'lost')) return;
        reasonSent = true;
        reasonButtons.forEach((item) => { item.disabled = true; });
        button.setAttribute('aria-pressed', 'true');
        thank(true);
        initialPost.then(() => postRating(unit, lesson, selectedRating, value));
      });
      reasonButtons.push(button);
      reasonOptions.append(button);
    });

    actions.append(options, status);
    const feedbackUrl = section.dataset.ratingFeedbackUrl;
    if (feedbackUrl) {
      const feedback = element('a', 'rating-feedback', 'ひとこと書く');
      feedback.href = feedbackUrl;
      feedback.target = '_blank';
      feedback.rel = 'noopener noreferrer';
      feedback.referrerPolicy = 'no-referrer';
      actions.append(feedback);
    }
    section.append(title, actions, reasons);
    if (savedDay() === localDay()) thank();
    section.hidden = false;
  };

  const showCounts = () => {
    const units = new Map();
    document.querySelectorAll('[data-rating-count-unit]').forEach((node) => {
      const unit = node.dataset.ratingCountUnit;
      if (!validUnit(unit)) return;
      if (!units.has(unit)) units.set(unit, []);
      units.get(unit).push(node);
    });
    units.forEach((nodes, unit) => {
      fetch(ENDPOINT + '?unit=' + encodeURIComponent(unit), {
        method: 'GET',
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
      }).then((response) => (response.ok ? response.json() : null)).then((data) => {
        if (!data || data.unit !== unit || !Number.isSafeInteger(data.got) || data.got <= 10) return;
        nodes.forEach((node) => {
          node.textContent = `この単元の「わかった」${data.got.toLocaleString('ja-JP')}回`;
          node.hidden = false;
        });
      }).catch(() => {});
    });
  };

  const start = () => {
    document.querySelectorAll('[data-rating-unit][data-rating-lesson]').forEach(createRating);
    showCounts();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
