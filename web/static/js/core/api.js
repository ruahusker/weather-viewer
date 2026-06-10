/**
 * WX Viewer — API core
 * Base-path resolution and the shared JSON fetch wrapper.
 */

// Resolve the app mount point (e.g. '/wx') from this module's own URL so
// API and asset paths work both at '/' and behind the /wx prefix.
export const BASE = (() => {
  try {
    const m = import.meta.url.match(/https?:\/\/[^/]+(\/[^?#]+?)\/static\//);
    if (m) {
      return m[1].replace(/\/+$/, '');
    }
    const path = window.location.pathname || '';
    if (path.startsWith('/wx/')) return '/wx';
    return '';
  } catch { return ''; }
})();

export async function apiFetch(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

export function assetPath(path) {
  if (!path) return '';
  if (/^https?:\/\//.test(path)) return path;
  return `${BASE}/${String(path).replace(/^\//, '')}`;
}
