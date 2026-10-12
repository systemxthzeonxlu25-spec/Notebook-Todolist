// Runs synchronously in <head> so the saved style and color mode apply before first paint.
(() => {
  'use strict';
  const STYLE_KEY = 'notebook-ui-style';
  const THEME_KEY = 'notebook-theme';
  const STYLES = ['claude', 'm3', 'hig'];
  const BAR_COLORS = {
    claude: { light: '#f5f3ee', dark: '#1f1e1c' },
    m3: { light: '#fef7ff', dark: '#141218' },
    hig: { light: '#f2f2f7', dark: '#000000' },
  };
  const read = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
  const write = (key, value) => {
    try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (_) { /* Storage may be disabled. */ }
  };
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  const root = document.documentElement;
  function style() { const value = read(STYLE_KEY); return STYLES.includes(value) ? value : 'claude'; }
  function mode() { const value = read(THEME_KEY); return value === 'dark' || value === 'light' ? value : 'system'; }
  function theme() { const value = mode(); return value === 'system' ? (media?.matches ? 'dark' : 'light') : value; }
  function apply() {
    const ui = style();
    const current = theme();
    root.dataset.ui = ui;
    root.dataset.theme = current;
    document.querySelectorAll('meta[name="theme-color"]').forEach(meta => { meta.content = BAR_COLORS[ui][current]; });
    document.dispatchEvent(new CustomEvent('appearance:change', { detail: { style: ui, mode: mode(), theme: current } }));
  }
  media?.addEventListener?.('change', () => { if (mode() === 'system') apply(); });
  window.addEventListener('storage', event => { if (event.key === STYLE_KEY || event.key === THEME_KEY) apply(); });
  window.Appearance = {
    styles: STYLES.slice(),
    style, mode, theme,
    setStyle(value) { write(STYLE_KEY, STYLES.includes(value) ? value : null); apply(); },
    setMode(value) { write(THEME_KEY, value === 'dark' || value === 'light' ? value : null); apply(); },
  };
  apply();
})();
