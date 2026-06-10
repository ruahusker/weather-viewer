/**
 * WX Viewer — safe localStorage access
 * localStorage throws in private-browsing / blocked-storage contexts;
 * these wrappers swallow those failures.
 */

export function storageGet(key, fallback = null) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch {
    return fallback;
  }
}

export function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {}
}
