/**
 * The "you" column — the cardholder's side, in the one app.
 *
 * Three jobs: show the mandate as the customer confirmed it, show the ledger
 * moving, and be the surface where a step-up is answered. Everything the
 * cardholder sees here is `customer_message` — never a reason code.
 */

import { html, el, chf, money, esc, raw } from '../core/format.js';
import { provText, sourceOf, SOURCE_LABEL } from '../core/provenance.js';
import { SETTING_META } from '../core/client.js';
import { bindingCap, facetOf, termsLabel } from '../core/policy.js';
import { createPrefsScreen } from './prefs-screen.js';
import * as ico from './icons.js';

const C = 2 * Math.PI * 30; // ring circumference for r=30

export function createPhonePanel(
  root,
  { onResolve, onTighten, onRevoke, onPreview, onEditSetting, onAcceptCandidate } = {},
) {
  const body = root.querySelector('.col__body');
  let timer = null;
  // 'wallet' is the ledger and the mandate; 'prefs' is the standing layer, which
  // outlives the mandate and so is reached from the header rather than from it.
  let screenMode = 'wallet';

  body.innerHTML = html`
    <div class="phone-wrap">
      <div class="phone">
        <div class="phone__screen">
          <div class="phone__status">9:41<span>one</span></div>
          <div class="phone__hd">
            <span class="phone__one">one</span>
            <span class="phone__ttl">${raw(ico.signet(28, { cut: 'small' }))}Leash</span>
            <button class="phone__prefs" data-prefs aria-label="Your preferences"
                    title="Your standing preferences — they outlive this errand">${raw(ico.sliders)}</button>
          </div>
          <div class="phone__body" data-screen></div>
          <div data-notif></div>
        </div>
      </div>
      <div class="controls" data-controls></div>
    </div>`;

  const screen = body.querySelector('[data-screen]');
  const notifSlot = body.querySelector('[data-notif]');
  const controls = body.querySelector('[data-controls]');
  let lastUsed = null;   // to notice the window rolling back
  let holdCap = true;    // keep a mid-drag slider value, but not across mandates
  let lastCap = null;    // so the cap row flashes on the render that changes it
  let lastControlCap = null; // to notice a widening, which invalidates a held slider value

  /* ── the ledger card ────────────────────────────────────────────────── */

  /**
   * The ledger figures, computed once per render. `freed` is how much the
   * rolling window gave back since the last render — the moment worth showing,
   * and the reason this is read from the engine rather than accumulated.
   */
  function ledgerFigures(win) {
    const used = Number(win.approved_spend_chf);
    const pending = Number(win.pending_step_up_chf);
    const limit = Number(win.limit_chf);
    const freed = lastUsed != null && used < lastUsed - 0.005 ? lastUsed - used : 0;
    lastUsed = used;
    return {
      used, pending, limit, freed,
      pct: Math.min(1, used / limit),
      pctP: Math.min(1, (used + pending) / limit),
      days: win.period_days,
    };
  }

  const ringTone = (pct) => (pct >= 1 ? 'over' : pct >= 0.8 ? 'near' : '');

  const ledgerSub = (l) =>
    (l.pending > 0 ? `CHF ${chf(l.pending)} waiting on you. ` : '') +
    (l.freed > 0
      ? `<b class="rolled">CHF ${chf(l.freed)} freed — earlier orders aged out of the window.</b>`
      : 'The window rolls: earlier orders age out and the room comes back.');

  /**
   * Move an existing ring rather than rebuilding it.
   *
   * A CSS transition needs a previous computed value to interpolate from, so a
   * freshly-created <circle> always snaps to its final offset. Rewriting the
   * card's markup on every decision therefore killed the animation outright —
   * the arc has to be the *same element* from one decision to the next.
   */
  function updateRing(root, state) {
    const l = ledgerFigures(state.window);
    const fill = root.querySelector('.ring__fill');
    const pend = root.querySelector('.ring__pending');

    fill.style.strokeDashoffset = C * (1 - l.pct);
    pend.style.strokeDashoffset = C * (1 - l.pctP);
    fill.classList.toggle('ring__fill--near', ringTone(l.pct) === 'near');
    fill.classList.toggle('ring__fill--over', ringTone(l.pct) === 'over');

    root.querySelector('.ring__pct').textContent = `${Math.round(l.pct * 100)}%`;
    root.querySelector('.ring-meta__k').textContent = `Last ${l.days} days`;
    root.querySelector('.ring-meta__v').innerHTML = `CHF ${esc(chf(l.used))} <em>of ${esc(chf(l.limit))}</em>`;
    root.querySelector('.ring-meta__sub').innerHTML = ledgerSub(l);
  }

  function ledgerCard(state) {
    const { window: win, counters } = state;
    if (win?.limit_chf) {
      const l = ledgerFigures(win);
      const tone = ringTone(l.pct);
      return html`
        <div class="ring-card">
          <div class="ring">
            <svg width="74" height="74" viewBox="0 0 74 74">
              <circle class="ring__track" cx="37" cy="37" r="30" fill="none" stroke-width="7"/>
              <circle class="ring__pending" cx="37" cy="37" r="30" fill="none" stroke-width="7"
                      stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - l.pctP)}" stroke-linecap="round"/>
              <circle class="ring__fill ${tone ? `ring__fill--${tone}` : ''}" cx="37" cy="37" r="30" fill="none" stroke-width="7"
                      stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - l.pct)}" stroke-linecap="round"/>
            </svg>
            <div class="ring__mid"><span class="ring__pct">${Math.round(l.pct * 100)}%</span><span class="ring__of">used</span></div>
          </div>
          <div class="ring-meta">
            <div class="ring-meta__k">Last ${l.days} days</div>
            <div class="ring-meta__v">CHF ${chf(l.used)} <em>of ${chf(l.limit)}</em></div>
            <div class="ring-meta__sub">${raw(ledgerSub(l))}</div>
          </div>
        </div>`;
    }
    const final = counters.final ?? {};
    const approved = final.approved ?? counters.approve ?? 0;
    const declined = final.declined ?? counters.decline ?? 0;
    const asked = final.waiting ?? counters.step_up ?? 0;
    return html`
      <div class="ring-card" style="gap:0;justify-content:space-between">
        ${raw(
          [
            ['Approved', approved, 'var(--ok)'],
            ['Declined', declined, 'var(--no)'],
            ['Needs you', asked, 'var(--brand-deep)'],
          ]
            .map(
              ([k, v, c]) => `<div style="text-align:center;flex:1">
                <div style="font-size:22px;font-weight:700;color:${c};line-height:1.1">${v}</div>
                <div style="font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--text-mute);font-weight:600;margin-top:2px;white-space:nowrap">${k}</div>
              </div>`,
            )
            .join(''),
        )}
      </div>`;
  }

  /* ── the mandate card ───────────────────────────────────────────────── */

  /**
   * The mandate, as rows the customer can tap.
   *
   * Each row names one setting from the catalogue, shows the value in force
   * across every layer, and says where that value came from — their own
   * sentence, their standing preferences, an edit they made, or the account.
   * With one layer "from “pay no more than CHF 200”" was the whole story;
   * with three, a row that does not name its source is a row the customer
   * cannot check.
   */
  function mandateCard(state) {
    const { mandate, activeCap, originalCap, revoked, policy } = state;
    const ir = mandate?.ir;
    if (!ir) return '';

    const capJustChanged = lastCap != null && activeCap !== lastCap;
    lastCap = activeCap;

    const effective = policy ?? { hard_rules: ir.rules, intent_facets: ir.intent_facets };
    const capRule = bindingCap(effective.hard_rules ?? ir.rules, 'purchase');

    const rows = [];
    rows.push({
      setting: 'per_order_limit_chf',
      ico: ico.wallet,
      k: 'Per-order limit',
      v: `CHF ${chf(activeCap)}`,
      changed: capJustChanged,
      prov: capRule
        ? provText(capRule)
        : activeCap !== originalCap
          ? `tightened from CHF ${chf(originalCap)} in the app`
          : provOf(ir, 'purchase'),
    });

    const period = (effective.hard_rules ?? ir.rules).find((r) => r.scope === 'period');
    if (period)
      rows.push({
        setting: 'period_limit_chf',
        ico: ico.clock,
        k: `Across ${period.period_days} days`,
        v: `CHF ${chf(period.value)}`,
        prov: provText(period),
      });

    for (const f of effective.intent_facets ?? ir.intent_facets ?? []) {
      rows.push({
        setting: settingForFacet(f.kind),
        ico: facetIcon(f.kind),
        k: facetLabel(f),
        v: '',
        prov: provText(f),
      });
    }

    rows.push({
      setting: 'uncertainty_policy',
      ico: ico.user,
      k: 'When unclear',
      v: uncertaintyWord(effective.uncertainty_policy ?? ir.uncertainty_policy),
      prov: provOf(ir, 'purchase') ? 'from “Ask me when uncertain”' : '',
    });

    return html`
      <div class="mandate">
        <header class="mandate__hd">
          ${raw(ico.lock)}
          <span class="mandate__ttl">Your mandate</span>
          <span class="mandate__state ${revoked ? 'mandate__state--revoked' : ''}">${revoked ? 'revoked' : 'active'}</span>
        </header>
        ${raw(rows
          .map((r) => {
            const editable = r.setting && !revoked;
            const tag = editable ? 'button' : 'div';
            const attrs = editable ? ` data-edit="${esc(r.setting)}"` : '';
            return `<${tag} class="mrow ${editable ? 'mrow--tap' : ''}"${attrs}>
              <span class="mrow__ico">${r.ico}</span>
              <span class="mrow__k">${esc(r.k)}${r.prov ? `<span class="prov">${esc(r.prov)}</span>` : ''}</span>
              ${r.v ? `<span class="mrow__v ${r.changed ? 'mrow__v--changed' : ''}">${esc(r.v)}</span>` : ''}
              ${editable ? `<span class="mrow__go">${ico.chevron}</span>` : ''}
            </${tag}>`;
          })
          .join(''))}
      </div>`;
  }

  const uncertaintyWord = (policy) =>
    ({ ask: 'Ask me', decline: 'Refuse it', approve: 'Allow it' })[policy] ?? policy;

  /** Which catalogue entry a facet row edits. Null means "not editable here". */
  const settingForFacet = (kind) =>
    Object.keys(SETTING_META).find((k) => SETTING_META[k].target === `facet:${kind}`) ?? null;

  const provOf = (ir, scope) => {
    const r = ir.rules.find((x) => x.scope === scope);
    return r ? provText(r) : '';
  };

  const facetIcon = (kind) =>
    ({
      merchant_familiarity: ico.shop,
      merchant_type: ico.shop,
      item_identity: ico.tag,
      item_attribute: ico.tag,
      order_terms: ico.doc,
      no_additions: ico.shield,
    }[kind] ?? ico.tag);

  function facetLabel(f) {
    const r = f.require ?? {};
    switch (f.kind) {
      case 'merchant_familiarity': {
        const min = Number(r.prior_approvals_min ?? 1);
        return min <= 1 ? 'Only shops you have used' : `Only shops you have used ${min}+ times`;
      }
      case 'merchant_type': return `Only ${(r.merchant_category_in ?? []).join(', ').replace(/_/g, ' ')} shops`;
      case 'item_identity': return `Only ${r.item_description ?? 'the item you asked for'}`;
      case 'item_attribute': return `Only ${Object.entries(r).map(([k, v]) => `${k} ${v}`).join(', ')}`;
      case 'order_terms':
      case 'category_exclusion':
      case 'spending_hours':
        return termsLabel(f.kind, r);
      case 'no_additions': return 'Nothing added you did not ask for';
      default: return f.kind.replace(/_/g, ' ');
    }
  }

  /* ── controls ───────────────────────────────────────────────────────── */

  function renderControls(state) {
    const { activeCap, revoked } = state;
    const min = 20;
    // Keep whatever the presenter had dialled in; a decision landing mid-drag
    // must not snap the slider back. But a *widening* confirmed in the sheet
    // changes the slider's whole range, so a value held from the old one is
    // stale rather than deliberate — drop it and show the cap now in force.
    const widened = lastControlCap != null && activeCap > lastControlCap;
    lastControlCap = activeCap;
    const held = holdCap && !widened ? controls.querySelector('[data-cap]') : null;
    const heldValue = held && Number(held.value) < activeCap ? held.value : null;
    // One compact card, not two stacked ones. The pair cost 197px of a 634px
    // column at a 800px viewport, which is height the customer's own screen
    // needs far more than these controls do — they are actions, wanted at one
    // moment each, and they only have to be one gesture away.
    controls.innerHTML = html`
      <div class="ctl">
        <div class="ctl__row">
          ${raw(ico.lock)}
          <input class="slider" type="range" min="${min}" max="${Math.round(activeCap)}"
                 value="${heldValue ?? Math.round(activeCap)}" step="10" data-cap
                 aria-label="Per-order limit" ${revoked ? 'disabled' : ''}>
          <span class="ctl__val" data-capval>CHF ${chf(heldValue ?? activeCap)}</span>
        </div>
        <div class="ctl__row ctl__row--acts">
          <p class="ctl__hint" data-caphint>Add-only: the cap can be narrowed, never widened.</p>
          <button class="btn btn--sm btn--primary" data-apply disabled>Apply</button>
          <button class="btn btn--sm btn--danger" data-revoke ${revoked ? 'disabled' : ''}
                  title="Revoking stops the agent. Runs are refused before a request reaches the control layer.">Revoke</button>
        </div>
      </div>`;

    const slider = controls.querySelector('[data-cap]');
    const valEl = controls.querySelector('[data-capval]');
    const hint = controls.querySelector('[data-caphint]');
    const apply = controls.querySelector('[data-apply]');

    slider?.addEventListener('input', () => {
      const v = Number(slider.value);
      valEl.textContent = `CHF ${chf(v)}`;
      const narrower = v < activeCap;
      apply.disabled = !narrower;
      if (!narrower) {
        hint.textContent = 'Add-only: the cap can be narrowed, never widened.';
        return;
      }
      const flips = onPreview?.(v) ?? 0;
      hint.innerHTML =
        flips > 0
          ? `<b style="color:var(--brand-deep)">${flips}</b> of the agent's remaining orders would be declined at CHF ${chf(v)}.`
          : `No remaining order changes at CHF ${chf(v)}.`;
    });
    apply?.addEventListener('click', () => onTighten?.(Number(slider.value)));
    if (heldValue) slider.dispatchEvent(new Event('input'));
    controls.querySelector('[data-revoke]')?.addEventListener('click', () => onRevoke?.());
  }

  /* ── step-up notification ───────────────────────────────────────────── */

  function showStepUp(record, { seconds = 120, tickMs = 1000 } = {}) {
    clearInterval(timer);
    let left = seconds;
    notifSlot.innerHTML = html`
      <div class="notif">
        <div class="notif__hd">
          <span class="notif__badge">${raw(ico.signet(15, { cut: 'px16', reverse: true }))}</span>
          <span class="notif__app">one · Leash</span>
          <span class="notif__ago">now</span>
        </div>
        <p class="notif__msg">${record.customer_message}</p>
        <div class="notif__timer">
          <div class="notif__bar"><i data-bar style="width:100%"></i></div>
          <span class="notif__secs" data-secs>${seconds}s</span>
        </div>
        <div class="notif__acts">
          <button class="btn btn--danger" data-no>Decline</button>
          <button class="btn btn--ok" data-yes>Approve</button>
        </div>
      </div>`;

    const bar = notifSlot.querySelector('[data-bar]');
    const secs = notifSlot.querySelector('[data-secs]');
    timer = setInterval(() => {
      left -= 1;
      bar.style.width = `${Math.max(0, (left / seconds) * 100)}%`;
      secs.textContent = `${Math.max(0, left)}s`;
      if (left <= 0) {
        clearInterval(timer);
        onResolve?.(record.authorization_id, 'decline', { expired: true });
      }
    }, tickMs);

    notifSlot.querySelector('[data-yes]').addEventListener('click', () => onResolve?.(record.authorization_id, 'approve'));
    notifSlot.querySelector('[data-no]').addEventListener('click', () => onResolve?.(record.authorization_id, 'decline'));
  }

  function hideStepUp() {
    clearInterval(timer);
    const n = notifSlot.querySelector('.notif');
    if (!n) return;
    n.classList.add('notif--leaving');
    setTimeout(() => (notifSlot.innerHTML = ''), 300);
  }

  /* ── render ─────────────────────────────────────────────────────────── */

  const prefsScreen = createPrefsScreen({
    onBack: () => {
      screenMode = 'wallet';
      screen.innerHTML = '';
      render(lastState ?? {});
    },
    onEdit: (setting) => onEditSetting?.(setting, { scope: 'preferences' }),
    onAccept: (candidate) => onAcceptCandidate?.(candidate),
  });

  let lastState = null;

  body.querySelector('[data-prefs]')?.addEventListener('click', () => {
    screenMode = screenMode === 'prefs' ? 'wallet' : 'prefs';
    screen.innerHTML = '';
    render(lastState ?? {});
  });

  function render(state, { reset = false } = {}) {
    lastState = state;
    if (reset) {
      lastUsed = null;
      lastCap = null;
      lastControlCap = null;
      screenMode = 'wallet';
      screen.innerHTML = '';
    }
    holdCap = !reset;
    body.querySelector('[data-prefs]')?.setAttribute('aria-pressed', String(screenMode === 'prefs'));

    if (screenMode === 'prefs') {
      screen.innerHTML = prefsScreen.render(state);
      prefsScreen.bind(screen, state);
      renderControls(state);
      holdCap = true;
      return;
    }

    if (!screen.firstElementChild) {
      screen.innerHTML = '<div data-ledger></div><div data-mandate></div>';
    }
    const ledgerSlot = screen.querySelector('[data-ledger]');

    // Keep the ring alive between decisions so its arc can transition; rebuild
    // the card only when there is no ring yet, or the scenario has no window.
    if (state.window?.limit_chf && ledgerSlot.querySelector('.ring')) {
      updateRing(ledgerSlot, state);
    } else {
      ledgerSlot.innerHTML = ledgerCard(state);
    }

    const mandateSlot = screen.querySelector('[data-mandate]');
    mandateSlot.innerHTML = mandateCard(state);
    mandateSlot.querySelectorAll('[data-edit]').forEach((b) =>
      b.addEventListener('click', () => onEditSetting?.(b.dataset.edit, { scope: 'mandate' })),
    );

    renderControls(state);
    holdCap = true;
  }

  return { render, showStepUp, hideStepUp, get screenMode() { return screenMode; } };
}
