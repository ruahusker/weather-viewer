/**
 * WX Viewer — formatting helpers
 * Pure functions: dates, relative times, HTML escaping.
 */

export function centralTime(date) {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Chicago',
    hour: 'numeric', minute: '2-digit', hour12: true,
  }).format(date);
}

export function parseIsoDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatMinutesAgo(value) {
  const date = parseIsoDate(value);
  if (!date) return '--';
  const minutes = Math.max(0, Math.round((Date.now() - date.getTime()) / 60000));
  return minutes < 1 ? '<1m ago' : `${minutes}m ago`;
}

export function formatMinutesLeft(minutes) {
  if (minutes === null || minutes === undefined) return '--';
  if (minutes < 60) return `${minutes}m left`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m left` : `${hours}h left`;
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
