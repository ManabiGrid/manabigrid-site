(function () {
  'use strict';

  const normalizeText = (value) => String(value || '')
    .normalize('NFKC')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();

  const resultPriority = {
    'レッスン': 0,
    '案内・設計図': 1,
    '解答': 2,
  };

  const buildContentResults = (query) => {
    const panel = document.querySelector('[data-content-search-results]');
    const list = document.querySelector('[data-content-search-list]');
    const index = Array.isArray(window.MANABIGRID_SEARCH_INDEX)
      ? window.MANABIGRID_SEARCH_INDEX
      : [];
    if (!panel || !list || query === '') {
      if (panel) panel.setAttribute('hidden', '');
      if (list) list.replaceChildren();
      return { total: 0, shown: 0 };
    }

    const matches = [];
    index.forEach((entry) => {
      const headings = Array.isArray(entry.headings) ? entry.headings : [];
      const heading = headings.find((item) => normalizeText(item.title).includes(query));
      const haystack = normalizeText([
        entry.title,
        entry.subject,
        entry.unit,
        ...headings.map((item) => item.title),
      ].join(' '));
      if (haystack.includes(query)) {
        matches.push({ entry, heading });
      }
    });
    matches.sort((left, right) => {
      const leftRank = resultPriority[left.entry.kind] ?? 9;
      const rightRank = resultPriority[right.entry.kind] ?? 9;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return String(left.entry.title || '').localeCompare(String(right.entry.title || ''), 'ja');
    });

    list.replaceChildren();
    if (matches.length === 0) {
      panel.setAttribute('hidden', '');
      return { total: 0, shown: 0 };
    }
    const visibleMatches = matches.slice(0, 24);
    visibleMatches.forEach(({ entry, heading }) => {
      const item = document.createElement('li');
      const link = document.createElement('a');
      const fragment = heading ? `#${encodeURIComponent(heading.anchor)}` : '';
      link.href = `../${entry.url}${fragment}`;
      link.textContent = heading ? `${entry.title} — ${heading.title}` : entry.title;
      const meta = document.createElement('span');
      meta.textContent = [entry.kind, entry.subject, entry.unit].filter(Boolean).join(' ／ ');
      item.append(link, meta);
      list.append(item);
    });
    panel.removeAttribute('hidden');
    return { total: matches.length, shown: visibleMatches.length };
  };

  const syncScrollableTables = () => {
    document.querySelectorAll('.table-wrap[data-scroll-label]').forEach((tableWrap) => {
      const scrollable = tableWrap.scrollWidth > tableWrap.clientWidth + 1;
      if (scrollable) {
        tableWrap.setAttribute('tabindex', '0');
        tableWrap.setAttribute('role', 'region');
        tableWrap.setAttribute('aria-label', tableWrap.dataset.scrollLabel || '横にスクロールできる表');
      } else {
        tableWrap.removeAttribute('tabindex');
        tableWrap.removeAttribute('role');
        tableWrap.removeAttribute('aria-label');
      }
    });
  };

  const scheduleScrollableTableSync = () => {
    requestAnimationFrame(syncScrollableTables);
  };

  const observeScrollableTables = () => {
    document.querySelectorAll('details').forEach((details) => {
      if (details.querySelector('.table-wrap[data-scroll-label]')) {
        details.addEventListener('toggle', scheduleScrollableTableSync);
      }
    });
    syncScrollableTables();
  };

  const createFilter = () => {
    const input = document.querySelector('[data-filter-input]');
    const items = Array.from(document.querySelectorAll('[data-search-item]'));
    if (!input || items.length === 0) {
      return;
    }

    const statusNode = document.querySelector('[data-filter-status]');
    const emptyState = document.querySelector('[data-empty-state]');
    const unit = statusNode ? statusNode.getAttribute('data-filter-unit') || '件' : '件';
    const hasContentSearch = input.hasAttribute('data-content-search-input');
    const disclosures = Array.from(document.querySelectorAll('[data-progress-disclosure]'));
    const queryParams = new URLSearchParams(window.location.search);
    const exactStatus = normalizeText(queryParams.get('status'));
    let initialHashApplied = false;

    const update = () => {
      const q = normalizeText(input.value);
      const activeExactStatus = exactStatus !== '' && q === exactStatus;
      let shown = 0;

      items.forEach((el) => {
        const target = normalizeText(el.getAttribute('data-search'));
        const rowStatus = normalizeText(el.getAttribute('data-status'));
        const canonicalRow = el.hasAttribute('data-progress-canonical-row');
        const matched = activeExactStatus
          ? canonicalRow && rowStatus === exactStatus
          : q === '' || target.includes(q);
        if (matched) {
          el.removeAttribute('hidden');
          shown += 1;
        } else {
          el.setAttribute('hidden', 'true');
        }
      });

      disclosures.forEach((details) => {
        const visibleRows = details.querySelectorAll('[data-search-item]:not([hidden])').length;
        const canonicalDisclosure = details.hasAttribute('data-progress-canonical');
        const countNode = details.querySelector('.disclosure-count');
        if (countNode && !countNode.dataset.totalLabel) {
          countNode.dataset.totalLabel = countNode.textContent || '';
        }
        if (countNode) {
          countNode.textContent = q !== '' ? `${visibleRows}行` : countNode.dataset.totalLabel;
        }
        details.hidden = activeExactStatus && !canonicalDisclosure;
        details.open = activeExactStatus
          ? canonicalDisclosure && visibleRows > 0
          : q !== '' && visibleRows > 0;
      });
      scheduleScrollableTableSync();

      const contentResult = hasContentSearch
        ? buildContentResults(q)
        : { total: 0, shown: 0 };
      const contentMatches = contentResult.total;
      if (statusNode) {
        if (shown === 0 && contentMatches === 0) {
          statusNode.textContent = `0${unit}。検索語を短くするか、消してください。`;
        } else if (hasContentSearch && q !== '') {
          const candidateStatus = contentResult.total > contentResult.shown
            ? `${contentResult.total}件中${contentResult.shown}件を表示`
            : `${contentResult.total}件`;
          statusNode.textContent = `${shown}件の教科・単元、本文候補${candidateStatus}`;
        } else {
          statusNode.textContent = `${shown}${unit}を表示`;
        }
      }
      if (emptyState) {
        if (shown === 0 && contentMatches === 0) {
          emptyState.removeAttribute('hidden');
        } else {
          emptyState.setAttribute('hidden', '');
        }
      }
      if (activeExactStatus && !initialHashApplied && window.location.hash) {
        initialHashApplied = true;
        const targetId = decodeURIComponent(window.location.hash.slice(1));
        requestAnimationFrame(() => {
          document.getElementById(targetId)?.scrollIntoView({ block: 'start' });
        });
      }
    };

    input.addEventListener('input', update);
    input.addEventListener('change', update);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && input.value !== '') {
        event.preventDefault();
        input.value = '';
        update();
      }
    });
    const query = queryParams.get('q') || queryParams.get('status');
    if (query !== null) {
      input.value = query;
    }
    update();
  };

  const createSectionObserver = () => {
    const links = Array.from(document.querySelectorAll('[data-section-link]'));
    if (links.length === 0 || !('IntersectionObserver' in window)) {
      return;
    }
    const byId = new Map();
    links.forEach((link) => {
      const id = link.getAttribute('data-section-link');
      if (!byId.has(id)) byId.set(id, []);
      byId.get(id).push(link);
    });
    const setActive = (id) => {
      links.forEach((link) => link.removeAttribute('aria-current'));
      (byId.get(id) || []).forEach((link) => link.setAttribute('aria-current', 'location'));
    };
    const observer = new IntersectionObserver((entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible[0]) setActive(visible[0].target.id);
    }, { rootMargin: '-12% 0px -72% 0px' });
    byId.forEach((_value, id) => {
      const heading = document.getElementById(id);
      if (heading) observer.observe(heading);
    });
  };

  const createPrintPreparation = () => {
    const disclosures = Array.from(document.querySelectorAll('[data-progress-disclosure]'));
    if (disclosures.length === 0) return;
    let openStates = null;
    window.addEventListener('beforeprint', () => {
      openStates = disclosures.map((details) => details.open);
      disclosures.forEach((details) => { details.open = true; });
    });
    window.addEventListener('afterprint', () => {
      if (!openStates) return;
      disclosures.forEach((details, index) => { details.open = openStates[index]; });
      openStates = null;
    });
  };

  const createSkipLink = () => {
    const link = document.querySelector('.skip-link[href="#main-content"]');
    const target = document.getElementById('main-content');
    if (!link || !target) return;
    link.addEventListener('click', () => {
      requestAnimationFrame(() => target.focus({ preventScroll: true }));
    });
  };

  const start = () => {
    createSkipLink();
    observeScrollableTables();
    createFilter();
    createSectionObserver();
    createPrintPreparation();
  };

  window.addEventListener('resize', scheduleScrollableTableSync, { passive: true });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
