'use strict';

/** Shared product favorites and readable run labels for both viewer layouts. */
(() => {
  const STORAGE_KEY = 'wx_favorite_products';
  const listeners = new Set();
  const validId = (id) => typeof id === 'string' && /^[a-z0-9_]{1,64}$/.test(id);
  function readFavorites() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
      return new Set(Array.isArray(saved) ? saved.filter(validId) : []);
    } catch { return new Set(); }
  }
  const state = { favorites: readFavorites() };
  const notify = () => listeners.forEach((listener) => listener());
  const favorites = {
    list: () => [...state.favorites],
    has: (id) => state.favorites.has(id),
    toggle(id) {
      if (!validId(id)) return;
      if (state.favorites.has(id)) state.favorites.delete(id);
      else state.favorites.add(id);
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify([...state.favorites])); } catch {}
      notify();
    },
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
  };
  window.addEventListener('storage', function syncFavorites(event) {
    if (event.key === STORAGE_KEY || event.key === null) {
      state.favorites = readFavorites();
      notify();
    }
  });
  function runStatus(run, older = false) {
    const progress = run.is_complete ? 'Complete' : run.completed_fxx?.length ? 'Updating' : 'Waiting';
    return older ? `Older · ${progress}` : progress;
  }
  window.ViewerUI = { favorites, runStatus };
})();
