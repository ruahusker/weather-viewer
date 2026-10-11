/**
 * WX Viewer — ES module entry point (desktop)
 *
 * Migration shim: core modules are real ES modules; legacy app.js (a
 * deferred classic script that executes after this module) consumes them
 * through the window.WX namespace and, during the transition, through
 * legacy global names. As feature code moves out of app.js the global
 * bindings shrink until only window.WX remains.
 */

import { state } from './core/state.js';
import { BASE, apiFetch, assetPath } from './core/api.js?v=20260709a';
import {
  centralTime, parseIsoDate, formatMinutesAgo, formatMinutesLeft, escapeHtml,
} from './core/format.js';
import { storageGet, storageSet, storageRemove } from './core/storage.js';

window.WX = {
  state,
  BASE,
  apiFetch,
  assetPath,
  centralTime,
  parseIsoDate,
  formatMinutesAgo,
  formatMinutesLeft,
  escapeHtml,
  storageGet,
  storageSet,
  storageRemove,
};

// Legacy global bindings for app.js (transition only).
Object.assign(window, {
  state,
  BASE,
  apiFetch,
  assetPath,
  centralTime,
  parseIsoDate,
  formatMinutesAgo,
  formatMinutesLeft,
  escapeHtml,
});
