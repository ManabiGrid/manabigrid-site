(function () {
  'use strict';

  const STORAGE_KEY = 'manabigrid-theme';
  const VALID_MODES = new Set(['system', 'light', 'dark']);
  const root = document.documentElement;
  const systemDark = window.matchMedia('(prefers-color-scheme: dark)');

  const readMode = () => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      return VALID_MODES.has(saved) ? saved : 'system';
    } catch (_error) {
      return 'system';
    }
  };

  let mode = readMode();

  const effectiveTheme = () => (
    mode === 'system' ? (systemDark.matches ? 'dark' : 'light') : mode
  );

  const applyTheme = () => {
    if (mode === 'system') {
      root.removeAttribute('data-theme');
    } else {
      root.setAttribute('data-theme', mode);
    }
    const color = effectiveTheme() === 'dark' ? '#101827' : '#ffffff';
    document.querySelector('meta[data-theme-color]')?.setAttribute('content', color);
    document.querySelectorAll('[data-theme-select]').forEach((select) => {
      select.value = mode;
    });
  };

  const saveMode = () => {
    try {
      if (mode === 'system') {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        window.localStorage.setItem(STORAGE_KEY, mode);
      }
    } catch (_error) {
      // The theme still applies to the current page when storage is unavailable.
    }
  };

  const start = () => {
    document.querySelectorAll('[data-theme-select]').forEach((select) => {
      select.value = mode;
      select.addEventListener('change', () => {
        mode = VALID_MODES.has(select.value) ? select.value : 'system';
        saveMode();
        applyTheme();
      });
    });
  };

  applyTheme();
  systemDark.addEventListener?.('change', () => {
    if (mode === 'system') applyTheme();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
