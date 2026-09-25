/** Presentation helpers. No business logic lives here. */

const CHF = new Intl.NumberFormat('de-CH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const chf = (v) => CHF.format(Number(v ?? 0));

export const money = (v, currency = 'CHF') =>
  `${currency} ${new Intl.NumberFormat('de-CH', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(v ?? 0))}`;

export function clock(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit' });
}

export function dayMonth(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString('de-CH', { day: '2-digit', month: 'short' });
}

export const sentence = (s) => (s ? s[0].toUpperCase() + s.slice(1) : '');

export const codeToWords = (code) => (code ? code.replace(/_/g, ' ') : '');

export function latency(ms) {
  if (ms == null) return '—';
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 100) return `${ms.toFixed(1)} ms`;
  return `${Math.round(ms)} ms`;
}

/** Escape for safe innerHTML interpolation. */
export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** `html` tagged template: escapes interpolations unless wrapped in raw(). */
const RAW = Symbol('raw');
export const raw = (s) => ({ [RAW]: String(s) });

export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    out += v && v[RAW] !== undefined ? v[RAW] : Array.isArray(v) ? v.map((x) => (x && x[RAW] !== undefined ? x[RAW] : esc(x))).join('') : esc(v);
    out += strings[i + 1];
  }
  return out;
}

/** Build an element from an html string. */
export function el(markup) {
  const t = document.createElement('template');
  t.innerHTML = markup.trim();
  return t.content.firstElementChild;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
