/**
 * The standing preferences screen — the customer, rather than one errand.
 *
 * Reached from the phone header rather than from the mandate, because it
 * outlives the mandate. A mandate is an errand and dies with it; this layer is
 * the person, and it is the half of the brief's *"customer-managed wallet
 * policy"* the build did not have.
 *
 * Four things on one screen:
 *
 *   1. what is set, and where each one came from
 *   2. what could be set, and which check would enforce it
 *   3. what the profile proposes — proposals a human accepts, never rules
 *   4. the one sentence that makes the layering visible without a diagram
 *
 * (3) matters more than it looks. `customers.csv` is prose written by the
 * organizers, not the customer's stated intent, so deriving rules from it is
 * the compile problem again with the same over-blocking risk — and without
 * even the excuse that the customer wrote the words. So the profile proposes
 * and the customer taps, and every proposal quotes the field it came from.
 *
 * Spec: wallet-control-layer/specs/customer-settings.md §8 and §10.3
 */

import { html, chf, esc, raw } from '../core/format.js';
import { SETTING_META, STANDING_SETTINGS } from '../core/client.js';
import { preferenceSentence } from '../core/policy.js';
import { SOURCE_LABEL } from '../core/provenance.js';
import * as ico from './icons.js';

const ICON = {
  per_order_limit_chf: ico.wallet,
  uncertainty_policy: ico.user,
  merchant_familiarity: ico.shop,
  merchant_type: ico.shop,
  order_terms: ico.doc,
  no_additions: ico.shield,
};

export function createPrefsScreen({ onEdit, onAccept, onClear, onBack }) {
  /**
   * @param {object} state  {preferences, rows, candidates, layers}
   */
  function render(state) {
    const prefs = state.preferences ?? {};
    const set = STANDING_SETTINGS.filter((k) => prefs[k] != null && prefs[k] !== false);
    const unset = STANDING_SETTINGS.filter((k) => !set.includes(k));
    const profile = state.candidates?.customer;

    return html`
      <div class="prefs">
        <header class="prefs__hd">
          <button class="prefs__back" data-back aria-label="Back">${raw(ico.back)}</button>
          <span class="prefs__ttl">Your preferences</span>
        </header>

        ${raw(profile ? personaCard(profile) : '')}
        ${raw(layeringLine(state))}

        <div class="prefs__sec">Standing rules${set.length ? '' : ' — none yet'}</div>
        ${raw(set.map((key) => row(key, prefs)).join(''))}

        ${raw(
          state.candidates?.candidates?.length
            ? `<div class="prefs__sec">From your profile</div>` +
              state.candidates.candidates
                .filter((c) => prefs[c.setting] == null)
                .map(candidateCard)
                .join('')
            : '',
        )}

        <div class="prefs__sec">Add a rule</div>
        ${raw(unset.map(addRow).join(''))}

        <details class="prefs__why">
          <summary>Why isn't everything here?</summary>
          <p>A setting no check reads would show on this screen as protection and protect
             nothing. These stay off until something enforces them:</p>
          <ul>
            ${raw(
              (state.candidates?.not_offered ?? [])
                .map((r) => `<li><b>${esc(r.field.replace(/_/g, ' '))}</b> — ${esc(r.why)}</li>`)
                .join(''),
            )}
          </ul>
        </details>
      </div>`;
  }

  const personaCard = (p) => `
    <div class="persona">
      <div class="persona__name">${esc(p.persona_name)}<span>${esc(p.customer_id)}</span></div>
      <p class="persona__line">${esc(p.background ?? '')}</p>
      <p class="persona__line persona__line--quiet">${esc(p.shopping_preferences ?? '')}</p>
    </div>`;

  /**
   * The whole architecture, in the customer's own screen, without a diagram.
   * Three numbers and which one applies.
   */
  function layeringLine(state) {
    const { mandateCap, preferenceCap, accountCap, effectiveCap } = state.layers ?? {};
    if (effectiveCap == null) return '';
    const parts = [];
    if (mandateCap != null) parts.push(`this errand asks for CHF ${chf(mandateCap)}`);
    if (preferenceCap != null) parts.push(`your preferences cap it at CHF ${chf(preferenceCap)}`);
    if (accountCap != null) parts.push(`your account at CHF ${chf(accountCap)}`);
    return `
      <p class="layerline">
        Your per-order limit is <b>CHF ${esc(chf(effectiveCap))}</b> —
        ${esc(parts.join(', '))}. The tightest one applies.
      </p>`;
  }

  const row = (key, prefs) => `
    <button class="prow" data-edit="${esc(key)}">
      <span class="prow__ico">${ICON[key] ?? ico.tag}</span>
      <span class="prow__k">${esc(SETTING_META[key].label)}
        <span class="prov">${esc(sentenceFor(key, prefs))}</span></span>
      <span class="prow__src">${esc(SOURCE_LABEL[prefs.origins?.[key]?.source ?? 'preferences'])}</span>
      ${ico.chevron}
    </button>`;

  const sentenceFor = (key, prefs) => preferenceSentence(key, prefs);

  const addRow = (key) => `
    <button class="prow prow--add" data-edit="${esc(key)}">
      <span class="prow__ico">${ico.plus}</span>
      <span class="prow__k">${esc(SETTING_META[key].label)}
        <span class="prov">${esc(SETTING_META[key].help)}</span></span>
      ${ico.chevron}
    </button>`;

  const candidateCard = (c) => `
    <div class="cand">
      <div class="cand__body">
        <div class="cand__ttl">${esc(c.sentence)}</div>
        <div class="cand__prov">from your profile: “${esc(c.quote)}”</div>
        <div class="cand__why">${esc(c.why)}</div>
      </div>
      <button class="btn btn--sm btn--ok" data-accept="${esc(c.setting)}">Accept</button>
    </div>`;

  /** Attach the handlers after the markup lands in the phone screen. */
  function bind(scope, state) {
    scope.querySelector('[data-back]')?.addEventListener('click', () => onBack?.());
    scope.querySelectorAll('[data-edit]').forEach((b) =>
      b.addEventListener('click', () => onEdit?.(b.dataset.edit)),
    );
    scope.querySelectorAll('[data-accept]').forEach((b) =>
      b.addEventListener('click', () => {
        const candidate = state.candidates.candidates.find((c) => c.setting === b.dataset.accept);
        onAccept?.(candidate);
      }),
    );
    scope.querySelectorAll('[data-clear]').forEach((b) =>
      b.addEventListener('click', () => onClear?.(b.dataset.clear)),
    );
  }

  return { render, bind };
}
