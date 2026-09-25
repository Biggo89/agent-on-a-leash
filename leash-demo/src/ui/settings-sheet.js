/**
 * The editor for one setting.
 *
 * Three parts, always in this order, and the order is the argument:
 *
 *   1. the control      — typed to the setting, not a generic text box
 *   2. the blast radius — "3 of the agent's 7 remaining orders would be
 *                          declined at CHF 250", computed before anything is
 *                          submitted
 *   3. the cost         — Apply, or Confirm a new mandate, or refused
 *
 * (2) is the part that makes this a policy editor rather than a form. A screen
 * that shows consequences before you commit is a different product from one
 * that does not.
 *
 * (3) is the pitch. Narrowing is one tap; widening costs a confirmation and
 * says so. The old slider could not even *express* a widening — its `max` was
 * the current cap — which enforced the rule and taught the customer nothing.
 *
 * Spec: wallet-control-layer/specs/customer-settings.md §10.2
 */

import { html, el, chf, esc, raw } from '../core/format.js';
import { SETTING_META } from '../core/client.js';
import * as ico from './icons.js';

const TONE = {
  tighten: { word: 'Apply', tone: 'ok', lead: 'This narrows what the agent may do.' },
  widen: {
    word: 'Confirm the change',
    tone: 'brand',
    lead: 'This widens what the agent may do, so it needs your confirmation.',
  },
  conflict: { word: 'Cannot apply', tone: 'danger', lead: '' },
  noop: { word: 'Apply', tone: 'ghost', lead: 'Nothing has changed yet.' },
};

export function createSettingsSheet(host) {
  let root = null;
  let session = null;

  function close() {
    if (!root) return;
    root.classList.add('sheet--leaving');
    const going = root;
    root = null;
    session = null;
    document.body.dataset.sheet = '';
    setTimeout(() => going.remove(), 220);
  }

  /**
   * @param {object} opts
   * @param {string} opts.setting    key in SETTING_META
   * @param {*}      opts.value      the value in force now
   * @param {string} opts.origin     where that value came from, for the subtitle
   * @param {(v:*) => object} opts.classify  → {kind, changes}
   * @param {(v:*) => object|null} opts.preview → {flips, ahead} or null
   * @param {(v:*) => Promise} opts.onApply
   */
  function open(opts) {
    close();
    const meta = SETTING_META[opts.setting];
    if (!meta) return;

    session = { ...opts, meta, value: opts.value };
    root = el(html`
      <div class="sheet" role="dialog" aria-modal="true" aria-label="${meta.label}">
        <div class="sheet__scrim" data-close></div>
        <div class="sheet__card">
          <header class="sheet__hd">
            <div>
              <h3 class="sheet__ttl">${meta.label}</h3>
              <p class="sheet__sub">${opts.origin ?? meta.help}</p>
            </div>
            <button class="sheet__x" data-close aria-label="Close">${raw(ico.close)}</button>
          </header>
          <div class="sheet__control" data-control></div>
          <p class="sheet__radius" data-radius></p>
          <div class="sheet__diff" data-diff></div>
          <footer class="sheet__acts">
            <p class="sheet__lead" data-lead></p>
            <button class="btn btn--sm btn--ghost" data-close>Cancel</button>
            <button class="btn btn--sm" data-apply disabled></button>
          </footer>
          <p class="sheet__check">Enforced by the <b>${esc(meta.check)}</b> check.</p>
        </div>
      </div>`);
    host.appendChild(root);
    document.body.dataset.sheet = '1';

    root.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', close));
    renderControl();
    refresh();
    root.querySelector('[data-apply]').addEventListener('click', async () => {
      const btn = root.querySelector('[data-apply]');
      btn.disabled = true;
      await session.onApply(session.value);
      close();
    });
  }

  /* ── the control, typed to the setting ─────────────────────────────── */

  function renderControl() {
    const slot = root.querySelector('[data-control]');
    const { meta, value } = session;

    if (meta.control === 'exclusions') {
      // Two lists in one control: the facet carries both dimensions and the check reads both,
      // so splitting them into two settings would give one facet two owners.
      const body = value ?? {};
      const chosen = {
        item_category_not_in: new Set(body.item_category_not_in ?? []),
        merchant_category_not_in: new Set(body.merchant_category_not_in ?? []),
      };
      const group = (key, label, options) => `
        <div class="exgroup">
          <div class="exgroup__ttl">${esc(label)}</div>
          <div class="catchips">
            ${options.map((c) => `<button class="catchip" data-ex="${esc(key)}" data-cat="${esc(c)}" aria-pressed="${String(chosen[key].has(c))}">${esc(String(c).replace(/_/g, ' '))}</button>`).join('')}
          </div>
        </div>`;
      slot.innerHTML =
        group('item_category_not_in', 'Things I never buy', meta.itemOptions) +
        group('merchant_category_not_in', 'Shops I never buy from', meta.shopOptions);
      const sync = () => {
        const next = {};
        for (const [key, set] of Object.entries(chosen)) if (set.size) next[key] = [...set].sort();
        session.value = next;
        refresh();
      };
      slot.querySelectorAll('[data-cat]').forEach((b) =>
        b.addEventListener('click', () => {
          const set = chosen[b.dataset.ex];
          const on = b.getAttribute('aria-pressed') === 'true';
          if (on) set.delete(b.dataset.cat);
          else set.add(b.dataset.cat);
          b.setAttribute('aria-pressed', String(!on));
          sync();
        }),
      );
      sync();
      return;
    }

    if (meta.control === 'hours') {
      const body = value ?? {};
      const state = { hours_from: Number(body.hours_from ?? 7), hours_to: Number(body.hours_to ?? 22) };
      const hh = (h) => String(h).padStart(2, '0');
      const picker = (key, label) => `
        <label class="hourpick">
          <span>${esc(label)}</span>
          <select data-hour="${esc(key)}">
            ${Array.from({ length: 24 }, (_, h) => `<option value="${h}" ${h === state[key] ? 'selected' : ''}>${hh(h)}:00</option>`).join('')}
          </select>
        </label>`;
      slot.innerHTML = `
        <div class="hours">${picker('hours_from', 'From')}${picker('hours_to', 'Until')}</div>
        <p class="hours__note" data-hoursnote></p>`;
      const note = slot.querySelector('[data-hoursnote]');
      const sync = () => {
        session.value = { ...state };
        // A window where `from > to` wraps midnight, which is how a person says quiet hours.
        note.textContent =
          state.hours_from === state.hours_to
            ? 'The start and end are the same, which could mean no hours or every hour.'
            : state.hours_from < state.hours_to
              ? `${hh(state.hours_from)}:00 until ${hh(state.hours_to)}:00, same day.`
              : `${hh(state.hours_from)}:00 until ${hh(state.hours_to)}:00 the next morning.`;
        refresh();
      };
      slot.querySelectorAll('[data-hour]').forEach((sel) =>
        sel.addEventListener('change', () => {
          state[sel.dataset.hour] = Number(sel.value);
          sync();
        }),
      );
      sync();
      return;
    }

    if (meta.control === 'money_window') {
      // A period rule with no window is `unknown` at decision time and refused on save, so
      // the amount and the window are one control rather than two fields one of which can
      // be left blank.
      const body = value ?? {};
      const amount = Number(body.value ?? 0);
      const chosen = Number(body.period_days ?? meta.windows[0]);
      slot.innerHTML = html`
        <div class="ctlrow">
          <button class="nudge" data-nudge="-50" aria-label="Less">−</button>
          <label class="amount">
            <span>CHF</span>
            <input type="number" min="1" step="50" value="${amount}" data-input inputmode="decimal">
          </label>
          <button class="nudge" data-nudge="50" aria-label="More">+</button>
        </div>
        <div class="seg" role="group" aria-label="Window">
          ${raw(meta.windows.map((d) => `<button class="seg__opt" data-window="${d}" aria-pressed="${String(d === chosen)}">${d} days</button>`).join(''))}
        </div>`;
      const input = slot.querySelector('[data-input]');
      const set = (next) => {
        session.value = { value: Math.max(1, Number(next.value) || 0), period_days: Number(next.period_days) };
        input.value = session.value.value;
        refresh();
      };
      input.addEventListener('input', () => set({ ...session.value, value: input.value }));
      slot.querySelectorAll('[data-nudge]').forEach((b) =>
        b.addEventListener('click', () => set({ ...session.value, value: Number(session.value.value) + Number(b.dataset.nudge) })),
      );
      slot.querySelectorAll('[data-window]').forEach((b) =>
        b.addEventListener('click', () => {
          slot.querySelectorAll('[data-window]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
          set({ ...session.value, period_days: b.dataset.window });
        }),
      );
      session.value = { value: amount, period_days: chosen };
      return;
    }

    if (meta.control === 'money') {
      const amount = Number(value ?? 0);
      slot.innerHTML = html`
        <div class="ctlrow">
          <button class="nudge" data-nudge="-10" aria-label="Less">−</button>
          <label class="amount">
            <span>CHF</span>
            <input type="number" min="1" step="10" value="${amount}" data-input inputmode="decimal">
          </label>
          <button class="nudge" data-nudge="10" aria-label="More">+</button>
        </div>
        <input class="slider" type="range" min="10" max="${Math.max(1000, Math.round(amount * 2))}"
               step="10" value="${amount}" data-range aria-label="${meta.label}">`;
      const input = slot.querySelector('[data-input]');
      const range = slot.querySelector('[data-range]');
      const set = (v) => {
        session.value = Math.max(1, Number(v) || 0);
        input.value = session.value;
        range.value = Math.min(Number(range.max), session.value);
        refresh();
      };
      input.addEventListener('input', () => set(input.value));
      range.addEventListener('input', () => set(range.value));
      slot.querySelectorAll('[data-nudge]').forEach((b) =>
        b.addEventListener('click', () => set(session.value + Number(b.dataset.nudge))),
      );
      return;
    }

    if (meta.control === 'integer') {
      const key = meta.key;
      const current = Number(value?.[key] ?? 0);
      slot.innerHTML = html`
        <div class="ctlrow">
          <button class="nudge" data-nudge="-1" aria-label="Less">−</button>
          <span class="amount amount--plain" data-readout>${current}${meta.unit ? ` ${meta.unit}` : ''}</span>
          <button class="nudge" data-nudge="1" aria-label="More">+</button>
        </div>`;
      const readout = slot.querySelector('[data-readout]');
      slot.querySelectorAll('[data-nudge]').forEach((b) =>
        b.addEventListener('click', () => {
          const next = Math.min(meta.max ?? 99, Math.max(meta.min ?? 0, Number(session.value?.[key] ?? 0) + Number(b.dataset.nudge)));
          session.value = { ...(session.value ?? {}), [key]: next };
          readout.textContent = `${next}${meta.unit ? ` ${meta.unit}` : ''}`;
          refresh();
        }),
      );
      return;
    }

    if (meta.control === 'choice') {
      slot.innerHTML = html`
        <div class="seg" role="group">
          ${raw(
            meta.options
              .map(
                (o) =>
                  `<button class="seg__opt" data-choice="${esc(o.value)}" aria-pressed="${String(o.value === value)}">${esc(o.label)}</button>`,
              )
              .join(''),
          )}
        </div>`;
      slot.querySelectorAll('[data-choice]').forEach((b) =>
        b.addEventListener('click', () => {
          session.value = b.dataset.choice;
          slot.querySelectorAll('[data-choice]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
          refresh();
        }),
      );
      return;
    }

    if (meta.control === 'categories') {
      const key = meta.key;
      const chosen = new Set(value?.[key] ?? []);
      slot.innerHTML = html`
        <div class="catchips">
          ${raw(
            (meta.options ?? [])
              .map(
                (c) =>
                  `<button class="catchip" data-cat="${esc(c)}" aria-pressed="${String(chosen.has(c))}">${esc(String(c).replace(/_/g, ' '))}</button>`,
              )
              .join(''),
          )}
        </div>`;
      slot.querySelectorAll('[data-cat]').forEach((b) =>
        b.addEventListener('click', () => {
          const on = b.getAttribute('aria-pressed') === 'true';
          if (on) chosen.delete(b.dataset.cat);
          else chosen.add(b.dataset.cat);
          b.setAttribute('aria-pressed', String(!on));
          session.value = { ...(session.value ?? {}), [key]: [...chosen].sort() };
          refresh();
        }),
      );
      return;
    }

    // flag
    slot.innerHTML = html`
      <button class="toggle" data-toggle aria-pressed="${String(Boolean(value))}">
        <i></i><span>${esc(SETTING_META[session.setting].label)}</span>
      </button>`;
    const toggle = slot.querySelector('[data-toggle]');
    toggle.addEventListener('click', () => {
      session.value = !session.value;
      toggle.setAttribute('aria-pressed', String(session.value));
      refresh();
    });
  }

  /* ── the blast radius and the cost ─────────────────────────────────── */

  function refresh() {
    const amendment = session.classify(session.value) ?? { kind: 'noop', changes: [] };
    const tone = TONE[amendment.kind] ?? TONE.noop;
    const apply = root.querySelector('[data-apply]');
    const lead = root.querySelector('[data-lead]');
    const radius = root.querySelector('[data-radius]');
    const diff = root.querySelector('[data-diff]');

    apply.textContent = tone.word;
    apply.className = `btn btn--sm btn--${tone.tone}`;
    apply.disabled = amendment.kind === 'conflict' || amendment.kind === 'noop';

    const conflict = amendment.changes.find((c) => c.kind === 'conflict');
    lead.textContent = conflict ? conflict.why : tone.lead;
    lead.className = `sheet__lead ${conflict ? 'sheet__lead--bad' : ''}`;

    diff.innerHTML = amendment.changes.length
      ? amendment.changes
          .map(
            (c) => `<div class="diffrow diffrow--${esc(c.kind)}">
              <span class="diffrow__k">${esc(c.label)}</span>
              <span class="diffrow__v">${esc(c.before ?? 'not set')} <i>→</i> ${esc(c.after ?? 'not set')}</span>
              <span class="diffrow__why">${esc(c.why)}</span>
            </div>`,
          )
          .join('')
      : '';

    // The engine cannot see the orders still ahead of a live run — the platform
    // delivers them one at a time — so this is a replay-mode affordance and the
    // copy says nothing rather than guessing.
    const preview = amendment.kind === 'noop' ? null : session.preview?.(session.value);
    radius.innerHTML = preview
      ? preview.flips > 0
        ? `<b>${preview.flips}</b> of the agent's ${preview.ahead} remaining order${preview.ahead === 1 ? '' : 's'} would be decided differently.`
        : `No remaining order changes.`
      : '';
  }

  return { open, close, get isOpen() { return Boolean(root); } };
}
