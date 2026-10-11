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

const DEFAULT_API_TIMEOUT_MS = 15_000;

export async function apiFetch(path, options = {}) {
  const {
    timeoutMs = DEFAULT_API_TIMEOUT_MS,
    signal: callerSignal,
    ...fetchOptions
  } = options;
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal) {
    if (callerSignal.aborted) abortFromCaller();
    else callerSignal.addEventListener('abort', abortFromCaller, { once: true });
  }
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const resp = await fetch(path, { ...fetchOptions, signal: controller.signal });
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    return await resp.json();
  } finally {
    clearTimeout(timeoutId);
    callerSignal?.removeEventListener('abort', abortFromCaller);
  }
}

export function assetPath(path) {
  if (!path) return '';
  if (/^https?:\/\//.test(path)) return path;
  return `${BASE}/${String(path).replace(/^\//, '')}`;
}
