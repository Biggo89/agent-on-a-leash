/**
 * The Cockpit — the cardholder's phone once Leash is set up.
 *
 * Four screens, all drawn here: the Cockpit (spent and available, the leash's state, the
 * open decisions count, recent agent orders), Open decisions (drafts an agent proposed, each
 * rule quoting the cardholder's own words, and step-ups with their countdown), Manage Leash
 * (rules in force, the per-order limit to tighten, connected agents with Revoke, what was
 * chosen when connecting, sign out) and Spending (every window, the account's limits).
 *
 * Reads the decision service exactly as the leash demo does — /connector/agents,
 * /connector/proposals, /v1/mandates, /v1/step-ups, /v1/decisions, /v1/preferences — plus the
 * app's own /api/me and /api/agents. Writes the cardholder's verbs and nothing else: confirm,
 * resolve, amend, revoke. None of them exists in a token an agent can carry.
 */

const CFG = window.WALLET; // { root, service, subject, card_id, name, icons, signet, persona, ... }
const S = CFG.service; // '' when the app is mounted on the service (same origin)
const R = CFG.root; // '' or '/app'
const I = CFG.icons;
const app = document.querySelector('[data-app]');
const toastHost = document.querySelector('[data-toasts]');

/* ── the service, in the shape the demo's http adapter uses ──────────────── */

async function call(path, { method = 'GET', body, base = S } = {}) {
  const res = await fetch(base + path, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const payload = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = new Error(payload?.error?.message ?? payload?.error_description ?? res.statusText);
    err.status = res.status;
    err.code = payload?.error?.code;
    throw err;
  }
  return payload;
}

const api = {
  agents: () => call('/connector/agents'),
  proposals: () => call('/connector/proposals'),
  mandates: () => call('/v1/mandates'),
  mandate: (id) => call(`/v1/mandates/${encodeURIComponent(id)}`),
  stepUps: () => call('/v1/step-ups'),
  decisions: () => call('/v1/decisions?limit=50'),
  confirm: (id) => call(`/v1/mandates/${encodeURIComponent(id)}/confirm`, { method: 'POST', body: { confirmed: true } }),
  reject: (id) => call(`/v1/mandates/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  resolve: (id, decision) =>
    call(`/v1/step-ups/${encodeURIComponent(id)}/resolve`, {
      method: 'POST',
      body: { decision, customer_message: 'Answered by the cardholder in the app.' },
    }),
  amend: (id, settings) => call(`/v1/mandates/${encodeURIComponent(id)}/amend`, { method: 'POST', body: { settings } }),
  me: () => call('/api/me', { base: R }),
  grants: () => call('/api/agents', { base: R }),
  revoke: (clientId) => call(`/api/agents/${encodeURIComponent(clientId)}/revoke`, { method: 'POST', base: R }),
};

/* ── small helpers ───────────────────────────────────────────────────────── */

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const CHF = new Intl.NumberFormat('de-CH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const chf = (v) => CHF.format(Number(v ?? 0));
const n = (v) => Number(v ?? 0);
const clock = (iso) => new Date(iso).toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit' });

function toast(text, tone = '') {
  const t = document.createElement('div');
  t.className = `toast${tone ? ` toast--${tone}` : ''}`;
  t.textContent = text;
  toastHost.appendChild(t);
  setTimeout(() => {
    t.classList.add('toast--out');
    setTimeout(() => t.remove(), 260);
  }, 3600);
}

/** Provenance entries of a rule or facet: a bare string is a quote of the instruction. */
function provenance(carrier) {
  const value = carrier?.provenance;
  if (value == null || value === '') return [];
  if (typeof value === 'string') return [{ source: 'instruction', quote: value }];
  if (Array.isArray(value)) return value.flatMap((v) => provenance({ provenance: v }));
  if (typeof value === 'object') return [{ source: value.source ?? 'instruction', quote: String(value.quote ?? '') }];
  return [];
}
const quotes = (carrier) => provenance(carrier).filter((e) => e.source === 'instruction' && e.quote).map((e) => e.quote);
const SOURCE_LABEL = { instruction: 'your instruction', preferences: 'your preferences', profile: 'your profile', amendment: 'an edit in the app', account: 'your account' };
function provText(carrier) {
  const entries = provenance(carrier);
  if (!entries.length) return '';
  return entries.map((e) => (e.source === 'instruction' ? `from “${e.quote}”` : e.quote || `from ${SOURCE_LABEL[e.source] ?? e.source}`)).join(' · ');
}

/** Wrap every listed substring found in `text`. */
function markSpans(text, spans) {
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
  for (const [a, b] of ranges.slice(1)) {
    const last = merged[merged.length - 1];
    if (a <= last[1]) last[1] = Math.max(last[1], b);
    else merged.push([a, b]);
  }
  let out = '';
  let cursor = 0;
  for (const [a, b] of merged) {
    out += esc(text.slice(cursor, a)) + `<mark>${esc(text.slice(a, b))}</mark>`;
    cursor = b;
  }
  return out + esc(text.slice(cursor));
}

/** decision-rules.md §9: the tightest cap of a scope. Ties break toward "<". */
function bindingCap(rules, scope) {
  const candidates = (rules ?? []).filter((r) => r?.field === 'billing_amount_chf' && r?.scope === scope && ['<=', '<'].includes(r?.operator));
  if (!candidates.length) return null;
  return candidates.reduce((best, r) => (n(r.value) < n(best.value) || (n(r.value) === n(best.value) && r.operator === '<') ? r : best));
}

const uncertaintyWord = (policy) => ({ ask: 'Ask me', decline: 'Refuse it', approve: 'Allow it' })[policy] ?? policy;

function facetLabel(f) {
  const r = f.require ?? {};
  switch (f.kind) {
    case 'merchant_familiarity': {
      const min = Number(r.prior_approvals_min ?? 1);
      return min <= 1 ? 'Only shops you have used' : `Only shops you have used ${min}+ times`;
    }
    case 'merchant_type':
      return `Only ${(r.merchant_category_in ?? []).join(', ').replace(/_/g, ' ')} shops`;
    case 'item_identity':
      return `Only ${r.item_description ?? 'the item you asked for'}`;
    case 'item_attribute':
      return `Only ${Object.entries(r).map(([k, v]) => `${k} ${v}`).join(', ')}`;
    case 'order_terms':
      return `Returnable within ${r.return_window_days_min}+ days`;
    case 'no_additions':
      return 'Nothing added you did not ask for';
    default:
      return String(f.kind).replace(/_/g, ' ');
  }
}
const facetIcon = (kind) => ({ merchant_familiarity: 'shop', merchant_type: 'shop', order_terms: 'doc', no_additions: 'shield' }[kind] ?? 'tag');

/** A mandate's rules as rows. */
function rowsFor(ir) {
  const rules = ir?.rules ?? ir?.hard_rules ?? [];
  const rows = [];
  const cap = bindingCap(rules, 'purchase');
  if (cap) rows.push({ ico: 'wallet', k: 'Per order', v: `up to CHF ${chf(cap.value)}`, s: provText(cap) });
  for (const r of rules.filter((x) => x.scope === 'period')) {
    rows.push({ ico: 'clock', k: `Across ${r.period_days} days`, v: `CHF ${chf(r.value)}`, s: provText(r) });
  }
  for (const f of ir?.intent_facets ?? []) rows.push({ ico: facetIcon(f.kind), k: facetLabel(f), v: '', s: provText(f) });
  rows.push({ ico: 'user', k: 'When something is unclear', v: uncertaintyWord(ir?.uncertainty_policy ?? 'ask'), s: '' });
  return rows;
}

const row = ({ ico, k, s = '', v = '', tone = '', acts = '' }) =>
  `<div class="row${s ? ' row--top' : ''}"><span class="row__ico${tone ? ` row__ico--${tone}` : ''}">${I[ico] ?? ''}</span>` +
  `<span class="row__body"><span class="row__k">${k}</span>${s ? `<span class="row__s">${s}</span>` : ''}</span>` +
  `${v ? `<span class="row__v">${v}</span>` : ''}${acts ? `<span class="row__acts">${acts}</span>` : ''}</div>`;
const kv = (k, v) => `<div class="kv"><span>${k}</span><span class="kv__v">${v}</span></div>`;
const mmss = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.max(0, Math.floor(seconds % 60))).padStart(2, '0')}`;

/* ── state ───────────────────────────────────────────────────────────────── */

const state = {
  view: 'cockpit',
  me: null,
  errand: null,
  errandsAll: [],
  mandateId: null,
  mandate: null, // { ir, instruction }
  drafts: [],
  proposals: [],
  grants: [],
  stepUps: [],
  decisions: [],
  offline: '',
};
const expiries = new Map(); // authorization_id → epoch ms the step-up closes
let mandateStale = true;
const draftCache = new Map();
let lastSignature = '';

function navigate(view) {
  state.view = view;
  lastSignature = '';
  render();
}

/* ── the screens ─────────────────────────────────────────────────────────── */

function header({ back = false, title = '' } = {}) {
  if (!back) {
    return `<header class="cp__hd"><button class="round" data-nav="details" aria-label="Spending">${I.search}</button>
<div class="pillbar"><button class="round" data-nav="decisions" aria-label="Open decisions">${I.mail}</button><button class="round" data-nav="manage" aria-label="Manage Leash">${I.user}</button><button class="round" data-nav="details" aria-label="Card">${I.card}</button></div></header>`;
  }
  return `<header class="hd"><button class="hd__back" data-nav="cockpit" aria-label="Back">${I.back}</button><div class="hd__brand">${CFG.signet.small}<span>${esc(title)}</span></div><span></span></header>`;
}

function tabs() {
  const tab = (ico, label, on = false) => `<button class="tab${on ? ' tab--on' : ''}" type="button" data-tab="${label}"><span class="tab__ico">${I[ico]}</span>${label}</button>`;
  return `<nav class="tabs">${tab('book', 'Cockpit', true)}${tab('bars', 'Analytics')}${tab('dots', 'one')}${tab('receipt', 'Invoices')}${tab('card', 'Card')}</nav>`;
}

function pendingStepUps() {
  const mine = new Set(state.errandsAll.filter((e) => e.subject === CFG.subject).map((e) => e.run_id));
  return state.stepUps.filter((x) => mine.has(x.run_id) && !x.expired && (x.seconds_remaining ?? 1) > 0);
}

function askCard(step) {
  const agent = state.errandsAll.find((e) => e.run_id === step.run_id)?.agent_name ?? 'Your agent';
  return `<section class="ask" data-ask="${esc(step.authorization_id)}"><div class="ask__hd"><span>${esc(agent)} asks</span><span class="countdown" data-countdown="${esc(step.authorization_id)}">${mmss(step.seconds_remaining ?? 0)}</span></div>
<p class="ask__ttl">CHF ${chf(step.amount_chf)} at ${esc(step.merchant_name ?? 'a shop')}</p>
<p class="ask__msg">${esc(step.customer_message ?? '')}</p>
<div class="seg"><button class="seg__b" type="button" data-resolve="decline" data-id="${esc(step.authorization_id)}">Decline</button><button class="seg__b seg__b--yes" type="button" data-resolve="approve" data-id="${esc(step.authorization_id)}">Approve once</button></div></section>`;
}

function spendCard() {
  const w = state.errand?.window;
  const spent = w?.approved_spend_chf != null ? n(w.approved_spend_chf) : sumApproved();
  const limit = w?.limit_chf != null ? n(w.limit_chf) : n(state.me?.limits?.monthly_limit_chf);
  const available = Math.max(0, limit - spent);
  const pct = limit > 0 ? Math.min(100, (spent / limit) * 100) : 0;
  const sub = w?.limit_chf != null ? `in the last ${w.period_days} days` : 'this month';
  return `<section class="card"><div class="spend"><div><div class="spend__k">Spent CHF</div><div class="spend__v">${chf(spent)}</div></div>
<div><div class="spend__k spend__k--r">Available CHF</div><div class="spend__v spend__v--r">${chf(available)}</div></div></div>
<div class="bar" title="${esc(sub)}"><div class="bar__fill" style="width:${pct.toFixed(1)}%"></div></div>
<div class="card__foot"><button class="link" type="button" data-nav="details">more details ${I.chevron}</button></div></section>`;
}

function sumApproved() {
  const runs = new Set(state.errandsAll.filter((e) => e.subject === CFG.subject).map((e) => e.run_id));
  return state.decisions.filter((d) => runs.has(d.run_id) && (d.final_status === 'approved' || (d.decision === 'approve' && !d.final_status))).reduce((t, d) => t + n(d.authorization?.billing_amount_chf), 0);
}

function leashStatus() {
  const e = state.errand;
  const agent = state.grants[0]?.agent_name ?? state.proposals[0]?.agent_name ?? 'your agent';
  if (e && e.status === 'running') return ['Leash active', 'Your agent payments are being checked.'];
  if (state.drafts.length) return ['A mandate to confirm', `${agent} proposed a mandate. Open it under Open decisions.`];
  if (state.grants.length) return ['Leash connected', `Waiting for your instruction to ${agent} in the chat.`];
  if (e) return ['Leash off', e.stopped_reason ? `Stopped: ${e.stopped_reason}.` : 'The errand is over.'];
  return ['Leash off', 'No agent is connected yet. Add the connector in Claude and sign in here.'];
}

function leashCard() {
  const [title, sub] = leashStatus();
  const ir = state.mandate?.ir;
  const rules = ir?.rules ?? ir?.hard_rules ?? [];
  const cap = bindingCap(rules, 'purchase');
  const periods = rules.filter((r) => r.scope === 'period');
  const open = state.drafts.length + pendingStepUps().length;
  let rows = '';
  if (cap) rows += kv('Per order', `up to CHF ${chf(cap.value)}`);
  for (const r of periods) rows += kv(`Across ${r.period_days} days`, `CHF ${chf(r.value)}`);
  if (!cap && !periods.length && state.me?.setup?.suggested_cap_chf) rows += kv('Suggested when connecting', `up to CHF ${state.me.setup.suggested_cap_chf}`);
  rows += kv('Open decisions', String(open));
  return `<section class="card"><div class="leash__hd"><span class="leash__mark">${CFG.signet.hero}</span><span class="row__body"><span class="row__k">${esc(title)}</span><span class="row__s">${esc(sub)}</span></span></div>
${rows}<div class="card__foot card__foot--between"><button class="link" type="button" data-nav="manage">Manage Leash</button><button class="link" type="button" data-nav="manage" aria-label="Manage Leash">${I.chevron}</button></div></section>`;
}

const CHIP = { approve: ['chip', 'approved'], decline: ['chip chip--no', 'declined'], step_up: ['chip chip--ask', 'asked you'] };

function decisionRow(d) {
  const a = d.authorization ?? {};
  let [cls, word] = CHIP[d.decision] ?? ['chip', d.decision];
  if (d.decision === 'step_up' && d.final_status) {
    [cls, word] = d.final_status === 'approved' ? ['chip', 'you approved'] : ['chip chip--no', d.final_status === 'expired' ? 'expired' : 'you declined'];
  }
  return `<div class="row row--top"><span class="${cls}">${esc(word)}</span><span class="row__body"><span class="row__k">${esc(a.merchant_name ?? '?')}</span><span class="row__s">${esc(String(d.customer_message ?? '').slice(0, 120))}</span></span><span class="row__v">CHF ${chf(a.billing_amount_chf)}<br><small>${d.decided_at ? clock(d.decided_at) : ''}</small></span></div>`;
}

function myDecisions() {
  const runs = new Set(state.errandsAll.filter((e) => e.subject === CFG.subject).map((e) => e.run_id));
  return state.decisions.filter((d) => runs.has(d.run_id));
}

function recentCard() {
  const rows = myDecisions().slice(0, 5);
  if (!rows.length) {
    return `<section class="card"><div class="empty">${I.card.replace('width="20" height="20"', 'width="44" height="30"')}Use your card and see your categorised transactions here.</div></section>`;
  }
  return `<section class="card"><p class="eyebrow card__eyebrow">Recent</p>${rows.map(decisionRow).join('')}</section>`;
}

function cockpitView() {
  return `${header()}<h1 class="h1">Cockpit</h1>${pendingStepUps().map(askCard).join('')}${spendCard()}${leashCard()}${recentCard()}${tabs()}`;
}

function draftCard(d) {
  const who = state.proposals.find((p) => p.draft_id === d.mandate_id) ?? {};
  const ir = d.ir ?? {};
  const spans = [...(ir.rules ?? []), ...(ir.intent_facets ?? [])].flatMap((c) => quotes(c));
  const asks = (ir.open_questions ?? d.open_questions ?? []).map((x) => row({ ico: 'bang', k: esc(x), tone: 'orange' })).join('');
  return `<section class="card">${row({ ico: 'bolt', k: `${esc(who.agent_name ?? 'An agent')} proposes a mandate`, v: '<span class="chip chip--ask">to confirm</span>' })}
<blockquote class="quote">“${markSpans(String(d.instruction ?? ir.source_instruction ?? ''), spans)}”</blockquote>
${rowsFor(ir).map((r) => row({ ico: r.ico, k: esc(r.k), v: esc(r.v), s: esc(r.s) })).join('')}${asks}
<div class="seg" style="padding:12px 0 14px"><button class="seg__b" type="button" data-reject="${esc(d.mandate_id)}">Decline</button><button class="seg__b" type="button" aria-pressed="true" data-confirm="${esc(d.mandate_id)}">Confirm</button></div></section>`;
}

function decisionsView() {
  const asks = pendingStepUps().map(askCard).join('');
  const drafts = state.drafts.map(draftCard).join('');
  const body = asks + drafts || `<section class="card"><div class="empty">${I.check.replace('width="20" height="20"', 'width="36" height="36"')}Nothing to decide right now.</div></section>`;
  return `${header({ back: true, title: 'Open decisions' })}${body}`;
}

function agentsCard() {
  const errands = Object.fromEntries(state.errandsAll.map((e) => [e.client_id, e]));
  const rows = state.grants.length
    ? state.grants
        .map((g) => {
          const e = errands[g.client_id];
          const w = e?.window;
          const spent = w?.limit_chf != null ? `CHF ${chf(w.approved_spend_chf)} of ${chf(w.limit_chf)} in ${w.period_days} days` : e ? `${e.orders} order${e.orders === 1 ? '' : 's'}` : 'no orders yet';
          const stateWord = e ? (e.status === 'running' ? 'on the leash' : e.status) : 'connected';
          return row({ ico: 'bolt', k: esc(g.agent_name), s: `${esc(stateWord)} · ${esc(spent)}`, tone: 'grey', acts: `<button class="btn btn--sm btn--danger" type="button" data-revoke="${esc(g.client_id)}">Revoke</button>` });
        })
        .join('')
    : `<p class="empty">No agent is connected. Add the connector in Claude, ChatGPT or Claude Code and sign in here.</p>`;
  return `<section class="card"><p class="eyebrow card__eyebrow">Connected agents</p>${rows}</section>`;
}

function rulesCard() {
  const m = state.mandate;
  if (!m) return `<section class="card"><p class="eyebrow card__eyebrow">Rules in force</p><p class="empty">No mandate yet. Tell your agent in the chat what to buy and within what limits; you confirm its proposal here.</p></section>`;
  const ir = m.ir ?? {};
  const spans = [...(ir.rules ?? []), ...(ir.intent_facets ?? [])].flatMap((c) => quotes(c));
  const cap = bindingCap(ir.rules ?? [], 'purchase');
  const field = cap
    ? `<div class="field"><span class="row__k" style="flex:none">Tighten per order</span><input type="number" min="1" step="1" inputmode="numeric" value="${Math.floor(n(cap.value))}" data-cap-input aria-label="Per-order limit in CHF"><button class="btn btn--sm btn--primary" type="button" data-cap-apply>Apply</button></div>`
    : '';
  return `<section class="card"><p class="eyebrow card__eyebrow">Rules in force</p>
<blockquote class="quote">“${markSpans(String(m.instruction ?? ''), spans)}”</blockquote>
${rowsFor(ir).map((r) => row({ ico: r.ico, k: esc(r.k), v: esc(r.v), s: esc(r.s) })).join('')}${field}</section>`;
}

function setupCard() {
  const s = state.me?.setup;
  if (!s) return '';
  const word = (c) => (c === 'allow' ? 'Allow automatically' : 'Ask me first');
  const labels = { A: 'Small purchase at a known merchant', B: 'Higher amount at a known merchant', C: 'A new merchant', D: 'Time-limited purchase' };
  return `<section class="card"><p class="eyebrow card__eyebrow">What you chose when connecting</p>
${kv('Past payments', s.used_history === false ? 'Not used' : 'Used to suggest the leash')}
${kv('Suggested per purchase', `up to CHF ${esc(s.suggested_cap_chf)}`)}
${Object.entries(s.choices ?? {}).map(([k, v]) => kv(labels[k] ?? k, word(v))).join('')}
<p class="note" style="text-align:left;padding:8px 0 12px">The rules that decide come from your instruction in the chat, confirmed here.</p></section>`;
}

function manageView() {
  return `${header({ back: true, title: 'Manage Leash' })}${rulesCard()}${agentsCard()}${setupCard()}
<form method="post" action="${esc(R)}/logout" class="acts"><button class="btn btn--secondary" type="submit">Sign out</button></form>`;
}

function detailsView() {
  const w = state.errand?.window;
  const windows = (w?.windows ?? []).map((x) => kv(`Last ${x.period_days} days`, `CHF ${chf(x.approved_spend_chf)}${x.limit_chf != null ? ` of ${chf(x.limit_chf)}` : ''} · ${x.approvals_in_window} approved`)).join('');
  const pending = w?.pending_step_up_chf && w.pending_step_up_chf !== '0.00' ? kv('Waiting for your answer', `CHF ${chf(w.pending_step_up_chf)}`) : '';
  const limits = state.me?.limits ?? {};
  const rows = myDecisions();
  return `${header({ back: true, title: 'Spending' })}
<section class="card"><p class="eyebrow card__eyebrow">Agent orders</p>${windows || kv('Approved by the agent', `CHF ${chf(sumApproved())}`)}${pending}</section>
<section class="card"><p class="eyebrow card__eyebrow">Your account</p>${kv('Per transaction', `CHF ${chf(limits.per_transaction_limit_chf)}`)}${kv('Per month', `CHF ${chf(limits.monthly_limit_chf)}`)}${kv('Card', `${esc(CFG.persona.card_label)} •••• ${esc(CFG.persona.last4)}`)}</section>
<section class="card"><p class="eyebrow card__eyebrow">All agent decisions</p>${rows.length ? rows.map(decisionRow).join('') : '<p class="empty">No orders yet.</p>'}</section>`;
}

/* ── render ──────────────────────────────────────────────────────────────── */

function signature() {
  return JSON.stringify(state, (k, v) => (k === 'seconds_remaining' || k === 'as_of' ? undefined : v));
}

function render() {
  const sig = signature();
  if (sig === lastSignature) return;
  lastSignature = sig;
  const views = { cockpit: cockpitView, decisions: decisionsView, manage: manageView, details: detailsView };
  app.innerHTML = (views[state.view] ?? cockpitView)() + (state.offline ? `<p class="status" data-state="off">engine offline · ${esc(state.offline)}</p>` : '');
}

function tickCountdowns() {
  const now = Date.now();
  for (const el of app.querySelectorAll('[data-countdown]')) {
    const until = expiries.get(el.dataset.countdown);
    if (until == null) continue;
    const left = Math.max(0, (until - now) / 1000);
    el.textContent = mmss(left);
    if (left <= 0) {
      expiries.delete(el.dataset.countdown);
      toast('The confirmation window closed. Nothing was charged.', 'warn');
      tick();
    }
  }
}

/* ── taps ────────────────────────────────────────────────────────────────── */

app.addEventListener('click', async (ev) => {
  const b = ev.target.closest('button');
  if (!b) return;
  if (b.dataset.nav) return navigate(b.dataset.nav);
  if (b.dataset.tab) {
    if (b.dataset.tab !== 'Cockpit') toast('Only the Cockpit is part of this mock.');
    return;
  }
  if (b.dataset.capApply !== undefined) {
    const input = app.querySelector('[data-cap-input]');
    const value = Number(input?.value);
    if (!(value > 0)) return toast('Enter an amount in CHF.', 'warn');
    return applyAmendment('per_order_limit_chf', value);
  }
  const act = b.dataset.resolve ? 'resolve' : b.dataset.confirm ? 'confirm' : b.dataset.reject ? 'reject' : b.dataset.revoke ? 'revoke' : null;
  if (!act) return;
  b.disabled = true;
  try {
    if (act === 'resolve') {
      const res = await api.resolve(b.dataset.id, b.dataset.resolve);
      expiries.delete(b.dataset.id);
      toast(`You ${res.status} it. Recorded as its own audit entry — the original decision is unchanged.`);
    } else if (act === 'confirm') {
      await api.confirm(b.dataset.confirm);
      draftCache.delete(b.dataset.confirm);
      toast('Mandate confirmed. Leash is active: your agent may shop within it now.');
    } else if (act === 'reject') {
      await api.reject(b.dataset.reject);
      draftCache.delete(b.dataset.reject);
      toast('Draft declined. Nothing is enforceable.');
    } else if (act === 'revoke') {
      const r = await api.revoke(b.dataset.revoke);
      const m = r.connector?.mandates_revoked?.length ?? 0;
      toast(`Revoked. ${r.tokens_revoked} token${r.tokens_revoked === 1 ? '' : 's'} gone${m ? `, ${m} mandate${m === 1 ? '' : 's'} revoked` : ''}. The agent's next call is refused.`);
    }
  } catch (e) {
    toast(e.message, 'warn');
    b.disabled = false;
  }
  return tick();
});

async function applyAmendment(setting, value) {
  if (!state.mandateId) return;
  try {
    const result = await api.amend(state.mandateId, { [setting]: value });
    if (result.applied) {
      mandateStale = true;
      toast("Per-order limit narrowed. It applies from the agent's next order; what you first agreed to stays on the record.");
    } else {
      toast('That would widen the mandate. Tell the agent the new limit in the chat; you confirm its proposal here.', 'warn');
    }
  } catch (e) {
    toast(e.code === 'preference_conflict' ? `Refused: ${e.message}` : e.message, 'warn');
  }
  return tick();
}

/* ── the loop ────────────────────────────────────────────────────────────── */

let ticking = false;
async function tick() {
  if (ticking) return;
  ticking = true;
  try {
    const [agents, proposals, mandates, stepUps, decisions, grants, me] = await Promise.all([
      api.agents(), api.proposals(), api.mandates(), api.stepUps(), api.decisions(), api.grants(), api.me(),
    ]);
    const errandsAll = agents.agents ?? [];
    const mine = errandsAll.filter((e) => e.subject === CFG.subject);
    const errand = mine.find((e) => e.status === 'running') ?? mine[mine.length - 1] ?? null;

    let { mandate, mandateId } = state;
    if (errand && (errand.mandate_id !== mandateId || mandateStale)) {
      const m = await api.mandate(errand.mandate_id);
      mandate = { ir: m.ir ?? {}, instruction: m.instruction ?? m.ir?.source_instruction ?? '' };
      mandateId = errand.mandate_id;
      mandateStale = false;
    } else if (!errand) {
      mandate = null;
      mandateId = null;
    }

    const byDraft = new Set((proposals.proposals ?? []).map((p) => p.draft_id));
    const drafts = [];
    for (const m of mandates.mandates ?? []) {
      // Only drafts an agent proposed through the connector, for this cardholder.
      if (m.status !== 'draft' || !byDraft.has(m.mandate_id)) continue;
      const who = (proposals.proposals ?? []).find((p) => p.draft_id === m.mandate_id);
      if (who && who.subject !== CFG.subject) continue;
      if (!draftCache.has(m.mandate_id)) draftCache.set(m.mandate_id, await api.mandate(m.mandate_id));
      drafts.push(draftCache.get(m.mandate_id));
    }

    for (const s of stepUps.step_ups ?? []) {
      if (!expiries.has(s.authorization_id) && s.seconds_remaining != null) {
        expiries.set(s.authorization_id, Date.now() + Number(s.seconds_remaining) * 1000);
      }
    }

    Object.assign(state, {
      me,
      errand,
      errandsAll,
      mandateId,
      mandate,
      drafts,
      proposals: proposals.proposals ?? [],
      grants: (grants.agents ?? []).filter((g) => g.subject === CFG.subject), // this cardholder's agents only
      stepUps: stepUps.step_ups ?? [],
      decisions: decisions.decisions ?? [],
      offline: '',
    });
  } catch (e) {
    state.offline = e.message;
  } finally {
    ticking = false;
  }
  render();
  tickCountdowns();
}

tick();
setInterval(tick, 2000);
setInterval(tickCountdowns, 1000);
