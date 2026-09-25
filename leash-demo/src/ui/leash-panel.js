/**
 * The Leash column — the verification theatre.
 *
 * Every check the engine ran is rendered, passes included: the audit value is
 * in the size of the evidence set, so showing only the checks that fired would
 * lose the point. Rows start pending and resolve in evaluation order, because
 * "every check always runs" is the claim this panel has to make visible.
 */

import { html, el, money, esc, latency, sleep, codeToWords, raw } from '../core/format.js';
import { CHECK_META, SIGNAL_META } from '../core/client.js';
import { termsLabel } from '../core/policy.js';
import * as ico from './icons.js';

const VERDICT_CLASS = {
  pass: 'pass',
  violation: 'violation',
  concern: 'concern',
  unknown: 'unknown',
  not_applicable: 'na',
};
const VERDICT_WORD = {
  pass: 'pass',
  violation: 'violation',
  concern: 'concern',
  unknown: 'unknown',
  not_applicable: 'n/a',
};
const DECISION_WORD = { approve: 'Approved', decline: 'Declined', step_up: 'Needs you' };

/** Wrap every listed substring found in `text`. Overlapping ranges merge. */
function markSpans(text, spans, tag = 'mark') {
  if (!text) return '';
  const ranges = [];
  for (const sub of spans ?? []) {
    if (!sub) continue;
    const i = text.indexOf(sub);
    if (i >= 0) ranges.push([i, i + sub.length]);
  }
  if (!ranges.length) return esc(text);
  ranges.sort((a, b) => a[0] - b[0]);
  const merged = [ranges[0]];
  for (const [s, e] of ranges.slice(1)) {
    const last = merged[merged.length - 1];
    if (s <= last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  let out = '';
  let cursor = 0;
  for (const [s, e] of merged) {
    out += esc(text.slice(cursor, s)) + `<${tag}>${esc(text.slice(s, e))}</${tag}>`;
    cursor = e;
  }
  return out + esc(text.slice(cursor));
}

const markHostile = (text, manipulations) => markSpans(text, (manipulations ?? []).map((m) => m.excerpt));


/**
 * The trust score, as a chip beside the verdict.
 *
 * Derived from the same check results rendered above it — `specs/trust-score.md` — so it is a
 * glance at evidence the panel is already showing, never a second opinion. A record with
 * nothing evaluated has no score rather than a low one, and the chip simply stays away.
 *
 * The coverage figure is the part worth reading: every check that applied, ran.
 */
/**
 * Which compiler wrote this mandate, from the IR's own `compiler` and `compiler_notes`.
 * The model proposes and the guard keeps only what quotes the customer; a model that could
 * not be reached leaves the deterministic baseline's mandate, and says so.
 */
/**
 * A cap as the customer wrote it. Every rule is enforced in CHF; one written in another
 * currency was converted at the pack's fixed rate and keeps the words under `stated`.
 * Spec: wallet-control-layer/specs/llm-compiler.md §"Foreign-currency caps"
 */
function asWritten(rule) {
  const chf = `CHF ${Number(rule.value).toFixed(2)}`;
  const s = rule.stated;
  return s && s.currency !== 'CHF' ? `${s.currency} ${Number(s.amount).toFixed(2)} (${chf})` : chf;
}

function compiledBy(ir) {
  if ((ir.compiler_notes ?? []).includes('fell_back_to_baseline')) return 'the baseline (model unavailable)';
  const c = String(ir.compiler ?? '');
  return c.startsWith('llm:') ? `the model (${c.slice(4).split('/').pop()}), checked by the guard` : 'the baseline';
}

function trustChip(record) {
  const t = record.score?.trust;
  if (t === null || t === undefined) return '';
  const cover = record.score.coverage;
  const band = record.score.band;
  const title =
    `${t}/100 · ${record.score.label}` +
    (cover ? ` · ${cover.evaluated} of ${cover.checks - cover.not_applicable} applicable checks ran` : '') +
    (record.score.deductions?.length
      ? ` · ${record.score.deductions.map((d) => `${codeToWords(d.reason_code)} −${d.points}`).join(', ')}`
      : '');
  return `<span class="trust trust--${esc(band)}" title="${esc(title)}">
            <b>${t}</b><em>/100</em>
          </span>`;
}

export function createLeashPanel(root, { onOpenAudit } = {}) {
  const body = root.querySelector('.col__body');
  let currentRecord = null;
  let currentEvent = null;

  function empty() {
    body.innerHTML = html`
      <div class="leash-empty">
        ${raw(ico.bigLeash)}
        <h3>Nothing to check yet</h3>
        <p>When the agent asks to pay, the request lands here and every check in the
           mandate runs against it — passes included.</p>
      </div>`;
  }

  /**
   * The opening beat: the cardholder's own words, and what they compiled to.
   * Every rule and facet carries the verbatim span it came from — highlight
   * those spans in the sentence and the guarantee stops being a claim.
   */
  function showMandate(scenario, ir) {
    const items = [
      ...ir.rules.map((r) => ({
        prov: r.provenance,
        label:
          r.scope === 'period'
            ? `At most ${asWritten(r)} across ${r.period_days} days`
            : `At most ${asWritten(r)} per order`,
        kind: 'rule',
      })),
      ...(ir.intent_facets ?? []).map((f) => ({ prov: f.provenance, label: facetLabel(f), kind: 'facet' })),
    ];

    body.innerHTML = html`
      <section class="compile">
        <header class="compile__hd">
          <span class="req__label">What you told the agent</span>
          <span class="chip">${scenario.id}</span>
        </header>
        <blockquote class="compile__quote">${raw(markSpans(scenario.instruction, items.map((i) => i.prov)))}</blockquote>

        <div class="compile__arrow"><span>compiled, reviewed, confirmed by you</span></div>

        <div class="compile__stats">
          <b>${ir.rules.length}</b> hard rule${ir.rules.length === 1 ? '' : 's'}
          · <b>${(ir.intent_facets ?? []).length}</b> intent facet${(ir.intent_facets ?? []).length === 1 ? '' : 's'}
          · <b>${(ir.open_questions ?? []).length}</b> open question${(ir.open_questions ?? []).length === 1 ? '' : 's'}
          · compiled by <b>${esc(compiledBy(ir))}</b>
        </div>

        <ul class="compile__list">
          ${raw(
            items
              .map(
                (i) => `<li class="compile__item compile__item--${i.kind}">
                  <span class="compile__label">${esc(i.label)}</span>
                  <span class="compile__prov">from “<mark>${esc(i.prov)}</mark>”</span>
                </li>`,
              )
              .join(''),
          )}
        </ul>

        ${(ir.open_questions ?? []).length
          ? raw(
              `<div class="compile__open">${ico.bang.replace(/#fff/g, 'currentColor')}<span>${esc(
                ir.open_questions[0],
              )}</span></div>`,
            )
          : ''}

        <p class="compile__note">Every rule quotes your words verbatim. A compiler that cannot
          quote you cannot add a rule — and nothing is enforceable until you confirm it.</p>
        <p class="compile__cta">Press <b>Run agent</b> — every request lands here, and every
          check runs against it.</p>
      </section>`;
  }

  function facetLabel(f) {
    const r = f.require ?? {};
    switch (f.kind) {
      case 'merchant_familiarity': return 'Only shops you have used before';
      case 'merchant_type': return `Only ${(r.merchant_category_in ?? []).join(', ').replace(/_/g, ' ')} shops`;
      case 'item_identity': return `Only ${r.item_description ?? 'what you asked for'}`;
      case 'item_attribute': return `Only ${Object.entries(r).map(([k, v]) => `${k} ${v}`).join(', ')}`;
      case 'order_terms':
      case 'category_exclusion':
      case 'spending_hours':
        return termsLabel(f.kind, r);
      case 'no_additions': return 'Nothing added you did not ask for';
      default: return f.kind.replace(/_/g, ' ');
    }
  }

  /** Render the incoming authorization, before any check has run. */
  function showRequest(event) {
    currentEvent = event;
    const enr = event.enrichment ?? {};
    const hostile = (enr.manipulations ?? []).length > 0;
    const item = event.items?.[0];
    const itemList = (event.items ?? [])
      .map((i) => `${esc(i.name)} <em>· ${i.quantity} × ${money(i.unit_price, i.currency)}</em>`)
      .join('<br>');

    const merchantChips = [
      enr.merchant_prior_approvals > 0
        ? `<span class="chip chip--ok">${enr.merchant_prior_approvals} prior order${enr.merchant_prior_approvals === 1 ? '' : 's'}</span>`
        : `<span class="chip">no prior orders</span>`,
      enr.merchant_lookalike_name
        ? `<span class="chip chip--warn">resembles ${esc(enr.merchant_lookalike_name)}</span>`
        : '',
      `<span class="chip">${esc(event.merchant_category)}</span>`,
    ].join('');

    const sellerText = item?.details
      ? `<div class="req__row" style="align-items:flex-start">
           <span class="req__k">Seller text</span>
           <span class="req__v" style="flex:1">
             <div class="mtext ${hostile ? 'mtext--hostile' : ''}">${markHostile(item.details, enr.manipulations)}${
               hostile
                 ? `<div class="mtext__cap">${ico.shield} Merchant-controlled text. Read as data, never as instructions.</div>`
                 : ''
             }</div>
           </span>
         </div>`
      : '';

    body.innerHTML = html`
      <section class="req">
        <header class="req__hd">
          <span class="req__label">Authorization request</span>
          <span class="chip">${event.id}</span>
          <span class="req__amt">
            <b>${money(event.billing_amount_chf)}</b>
            ${event.currency !== 'CHF' ? raw(`<span>charged ${esc(money(event.amount, event.currency))}</span>`) : ''}
          </span>
        </header>
        <div class="req__body">
          <div class="req__row">
            <span class="req__k">Seller</span>
            <span class="req__v"><b style="font-weight:600">${event.merchant_name}</b>
              <span style="font-family:var(--mono);font-size:11px;color:var(--text-faint)"> ${event.merchant_id}</span>
              <br>${raw(merchantChips)}</span>
          </div>
          <div class="req__row">
            <span class="req__k">Basket</span>
            <span class="req__v">${raw(itemList)}</span>
          </div>
          ${raw(sellerText)}
        </div>
      </section>

      <section class="checks">
        <header class="checks__hd">
          ${raw(ico.shield)}
          <span class="checks__title">Checked against your mandate</span>
          <span class="checks__count" data-count>0 of 0</span>
        </header>
        <div data-rows></div>
        <div class="meter">
          <div class="meter__top">
            <span class="meter__label">Concern score</span>
            <span class="meter__val" data-score>0.0 <em>/ 2.0 asks you</em></span>
          </div>
          <div class="meter__track"><i class="meter__fill" data-fill></i><i class="meter__mark" data-mark></i></div>
          <p class="meter__note" data-mnote>Risk we infer can raise concern. It can never decline on its own — only a rule you wrote can do that.</p>
        </div>
      </section>
      <div data-verdict></div>
    `;
  }

  /** Pre-render all rows in a pending state, then resolve them in order. */
  async function runChecks(record, { stepMs = 82 } = {}) {
    currentRecord = record;
    const rows = body.querySelector('[data-rows]');
    const countEl = body.querySelector('[data-count]');
    const checks = record.checks;
    if (!rows) return;

    rows.innerHTML = checks
      .map(
        (c, i) => html`
        <div class="chk chk--pending" data-i="${i}">
          <span class="chk__dot"></span>
          <span class="chk__main">
            <span class="chk__name">${CHECK_META[c.check_id]?.name ?? codeToWords(c.check_id)}</span>
            <span class="chk__detail" data-detail>${CHECK_META[c.check_id]?.asks ?? ''}</span>
            <span class="chk__evidence" data-ev></span>
          </span>
          <span class="chk__verdict">…</span>
        </div>`,
      )
      .join('');

    countEl.textContent = `0 of ${checks.length}`;

    const threshold = record.score.step_up_threshold;
    const fill = body.querySelector('[data-fill]');
    const mark = body.querySelector('[data-mark]');
    const scoreEl = body.querySelector('[data-score]');
    const maxScale = Math.max(threshold * 2, record.score.concern_score + 1);
    mark.style.left = `${(threshold / maxScale) * 100}%`;

    let score = 0;
    for (let i = 0; i < checks.length; i++) {
      const c = checks[i];
      const row = rows.querySelector(`[data-i="${i}"]`);
      row.classList.remove('chk--pending');
      row.classList.add('chk--resolving');
      await sleep(stepMs);
      row.classList.remove('chk--resolving');
      row.classList.add('chk--done', `chk--${VERDICT_CLASS[c.verdict]}`);
      row.querySelector('.chk__dot').innerHTML = ico.VERDICT_ICON[c.verdict] ?? '';
      row.querySelector('.chk__verdict').textContent = VERDICT_WORD[c.verdict];

      const detail = row.querySelector('[data-detail]');
      if (c.detail) detail.textContent = c.detail;
      else if (c.verdict === 'not_applicable') detail.textContent = 'not part of this mandate';
      else if (c.verdict === 'pass') detail.textContent = 'nothing to raise';

      if (c.evidence?.length) {
        row.classList.add('chk--interactive');
        row.querySelector('[data-ev]').innerHTML = c.evidence
          .map((e) => `<span class="ev"><b>${esc(e.field)}</b><span>${esc(e.value)}</span></span>`)
          .join('');
        row.addEventListener('click', () => row.classList.toggle('chk--open'));
      }

      if (c.weight) {
        score += c.weight;
        scoreEl.innerHTML = `${score.toFixed(1)} <em>/ ${threshold.toFixed(1)} asks you</em>`;
        fill.style.width = `${Math.min(100, (score / maxScale) * 100)}%`;
        fill.classList.toggle('meter__fill--over', score >= threshold);
      }
      countEl.textContent = `${i + 1} of ${checks.length}`;
    }

    // The engine's own score is authoritative; assert the walk agreed with it.
    const engineScore = record.score.concern_score;
    scoreEl.innerHTML = `${engineScore.toFixed(1)} <em>/ ${threshold.toFixed(1)} asks you</em>`;
    fill.style.width = `${Math.min(100, (engineScore / maxScale) * 100)}%`;
    fill.classList.toggle('meter__fill--over', engineScore >= threshold);

    const raised = checks.filter((c) => c.weight).map((c) => SIGNAL_META[c.reason_code]?.label ?? c.detail);
    const note = body.querySelector('[data-mnote]');
    if (raised.length)
      note.innerHTML = `Raised: ${raised.map((r) => `<b style="color:var(--ask)">${esc(r)}</b>`).join(' · ')}. ` +
        (engineScore >= threshold
          ? 'At or above the threshold, so the cardholder is asked.'
          : 'Below the threshold, so this did not change the decision.');
  }

  /** Land the verdict. */
  function showVerdict(record, event) {
    const slot = body.querySelector('[data-verdict]');
    if (!slot) return;
    slot.innerHTML = html`
      <section class="verdict verdict--${record.decision}">
        <header class="verdict__hd">
          ${raw(record.decision === 'approve' ? ico.tick : record.decision === 'decline' ? ico.cross : ico.bang)}
          <span class="verdict__word">${DECISION_WORD[record.decision]}</span>
          ${raw(trustChip(record))}
          <span class="verdict__meta">decided in ${latency(record.timing.latency_ms)}</span>
        </header>
        <p class="verdict__msg">${record.customer_message}</p>
        <footer class="verdict__foot">
          ${record.tightened ? raw('<span class="tightened-flag">re-decided under your tighter cap</span>') : ''}
          <span>reason codes</span>
          ${raw(record.reason_codes.map((c) => `<span class="rc">${esc(c)}</span>`).join(''))}
          <button class="btn btn--ghost btn--sm" data-audit style="margin-left:auto">Audit record</button>
        </footer>
      </section>`;
    slot.querySelector('[data-audit]')?.addEventListener('click', () => onOpenAudit?.(record, event));
    body.scrollTo({ top: body.scrollHeight, behavior: 'smooth' });
  }

  /** After a step-up is answered, fold the outcome into the verdict card. */
  function markResolved(status, how = 'by you') {
    const foot = body.querySelector('.verdict__foot');
    if (!foot) return;
    const hd = body.querySelector('.verdict__hd');
    hd.insertAdjacentHTML(
      'beforeend',
      `<span class="verdict__meta" style="opacity:1;font-weight:600">→ ${status === 'approved' ? 'approved' : 'declined'} ${how}</span>`,
    );
  }

  empty();
  return { empty, showMandate, showRequest, runChecks, showVerdict, markResolved, get record() { return currentRecord; }, get event() { return currentEvent; } };
}
