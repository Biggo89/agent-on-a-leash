/**
 * Leash — the demo shell and the run loop.
 *
 * The loop is deliberately the only place that knows about timing. Everything
 * else takes data and renders it, so pointing the app at the real service is a
 * one-line change in `createClient` and nothing here moves.
 */

import { createMockClient } from '../adapters/mock.js';
import { createHttpClient, probe } from '../adapters/http.js';
import { createStore } from '../core/store.js';
import { html, el, sleep, chf, esc, raw } from '../core/format.js';
import { agentSteps, agentReaction } from '../data/agent-script.js';
import { createAgentPanel } from './agent-panel.js';
import { createLeashPanel } from './leash-panel.js';
import { createPhonePanel } from './phone-panel.js';
import { createAuditDrawer } from './audit.js';
import { createSettingsSheet } from './settings-sheet.js';
import {
  applySettings,
  classify,
  preferencesToPolicy,
  readSetting,
} from '../core/policy.js';
import { SETTING_META } from '../core/client.js';
import { SOURCE_LABEL, sourceOf } from '../core/provenance.js';
import * as ico from './icons.js';

/* ── state ─────────────────────────────────────────────────────────────── */

const store = createStore({
  scenarios: [],
  scenarioId: null,
  run: null,
  mandate: null,
  originalCap: 0,
  activeCap: 0,
  counters: {},
  window: null,
  revoked: false,
  running: false,
  speed: 1,
  live: false,
  // The composed policy the engine is actually judging against — account
  // ceiling, standing preferences, mandate. The mandate card reads it so a row
  // can say *which* layer set the value in force.
  policy: null,
  preferences: {},
  candidates: null,
  layers: {},
});

let client = createMockClient();
let loopToken = 0;
let awaitingHuman = null;
let inFlight = false; // one authorization at a time — Step is easy to double-tap
let upstreamLabel = 'upstream unknown'; // what the live engine is pointed at, from /healthz
const decisionsById = new Map();

const speedOf = () => store.get().speed;
const paced = (ms) => sleep(ms / speedOf());

/* ── chrome ────────────────────────────────────────────────────────────── */

const app = document.getElementById('app');
app.innerHTML = html`
  <header class="hdr">
    <a class="brand" href="#" style="text-decoration:none" aria-label="LEASH by one">
      ${raw(ico.lockup('brand__lockup'))}
      ${raw(ico.wordmark('brand__word'))}
    </a>
    <span class="hdr__tag">Let the agent shop. You still hold the leash.</span>
    <span class="hdr__spacer"></span>
    <div class="source" role="group" aria-label="Decision source">
      <button class="source__opt" data-src="mock" aria-pressed="true"><i class="source__dot"></i>Replay</button>
      <button class="source__opt" data-src="live" aria-pressed="false" aria-disabled="true"
        title="No engine answers on 127.0.0.1:8000 — press for how to start one"><i class="source__dot"></i
        ><span class="source__full">Live engine</span><span class="source__short">Live</span></button>
    </div>
  </header>

  <div class="transport">
    <div class="scen">
      <select class="scen__pick" data-scenario aria-label="Scenario"></select>
      <span class="scen__q" data-question></span>
    </div>
    <span class="transport__spacer"></span>
    <div class="panels" role="tablist" aria-label="Panel">
      <button class="panels__opt" data-panel="agent" role="tab" aria-selected="false">Agent</button>
      <button class="panels__opt" data-panel="leash" role="tab" aria-selected="true">Leash</button>
      <button class="panels__opt" data-panel="you" role="tab" aria-selected="false">You</button>
    </div>
    <span class="transport__break"></span>
    <div class="progress">
      <span data-progress-text>0 / 0</span>
      <span class="progress__track"><i class="progress__fill" data-progress-fill style="width:0%"></i></span>
    </div>
    <div class="ctrls">
      <div class="speed" role="group" aria-label="Speed">
        <button data-speed="1" aria-pressed="true">1×</button>
        <button data-speed="2" aria-pressed="false">2×</button>
        <button data-speed="4" aria-pressed="false">4×</button>
      </div>
      <button class="btn" data-step title="One order (→)">${raw(ico.step)}<span class="btn__label">Step</span></button>
      <button class="btn btn--ghost" data-reset title="Reset (r)">${raw(ico.reset)}<span class="btn__label">Reset</span></button>
      <button class="btn btn--brand" data-run title="Run / pause (space)">${raw(ico.play)}<span class="btn__label">Run agent</span></button>
    </div>
  </div>

  <main class="stage">
    <section class="col col--agent">
      <header class="col__hd"><span class="col__eyebrow">Agent</span><span class="col__sub">understands intent</span></header>
      <div class="col__body"></div>
      <footer class="col__foot">
        <b>The agent's steps are reconstructed</b> from each order's own facts — merchant, basket,
        prices, history — because the data pack records what an agent tried to pay for, not the
        browsing before it. Everything in the Leash column is engine output.
      </footer>
    </section>
    <section class="col col--leash">
      <header class="col__hd"><span class="col__eyebrow">The Leash</span><span class="col__sub">checks &amp; enforces</span></header>
      <div class="col__body"></div>
    </section>
    <section class="col col--you">
      <header class="col__hd"><span class="col__eyebrow">You</span><span class="col__sub">stay in control</span></header>
      <div class="col__body"></div>
    </section>
  </main>
  <div class="toasts" data-toasts></div>
`;

const q = (s) => app.querySelector(s);
const toastHost = q('[data-toasts]');

const audit = createAuditDrawer(document.body);
const agentPanel = createAgentPanel(q('.col--agent'));
const leashPanel = createLeashPanel(q('.col--leash'), {
  onOpenAudit: (record, event) =>
    audit.open(record, event, { resolution: decisionsById.get(record.authorization_id)?.finalStatus }),
});
const settingsSheet = createSettingsSheet(document.body);
const phonePanel = createPhonePanel(q('.col--you'), {
  onResolve: resolveStepUp,
  onTighten: tighten,
  onRevoke: revoke,
  onPreview: (cap) => client._internal?.previewCap?.(store.get().run?.run_id, cap) ?? 0,
  onEditSetting: openSetting,
  onAcceptCandidate: acceptCandidate,
});

function toast(text, tone = '') {
  const t = el(html`<div class="toast ${tone ? `toast--${tone}` : ''}">${text}</div>`);
  toastHost.appendChild(t);
  setTimeout(() => {
    t.classList.add('toast--out');
    setTimeout(() => t.remove(), 260);
  }, 3600);
}

/* ── wiring ────────────────────────────────────────────────────────────── */

q('[data-run]').addEventListener('click', () => (store.get().running ? pause() : run()));
q('[data-step]').addEventListener('click', () => stepOnce());
/**
 * Leave the current run for another. Live, only once it has finished or been revoked: the
 * platform serves one queue per team, and a run left behind keeps using it — its next
 * step-up would hold the new run's orders until they missed their deadline (http.js `busy`).
 */
async function leaveFor(scenarioId) {
  const s = store.get();
  if (s.live && !s.revoked && (await client.busy?.(s.run?.run_id))) {
    q('[data-scenario]').value = s.scenarioId;
    toast('This errand is still running on the live platform. Let it finish, or revoke it, before starting another.', 'warn');
    return;
  }
  await start(scenarioId);
}

q('[data-reset]').addEventListener('click', () => leaveFor(store.get().scenarioId));
q('[data-scenario]').addEventListener('change', (e) => leaveFor(e.target.value));

const stage = q('.stage');

/**
 * Below the three-column breakpoint the stage shows one panel at a time.
 * The class is always applied; CSS decides whether it means anything, so the
 * choice survives a resize in either direction.
 */
function showPanel(name) {
  stage.dataset.panel = name;
  app.querySelectorAll('.panels__opt').forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.panel === name)),
  );
}
showPanel('leash');

app.querySelectorAll('.panels__opt').forEach((b) =>
  b.addEventListener('click', () => showPanel(b.dataset.panel)),
);

/** A step-up is the one moment the cardholder's panel must not be hidden. */
function flagWaiting(waiting) {
  app.querySelector('.panels__opt[data-panel="you"]').dataset.waiting = waiting ? '1' : '';
  // On a phone the confirmation is a fixed sheet, so the scrolling column has
  // to make room for it or the last controls sit permanently underneath.
  document.body.dataset.stepup = waiting ? '1' : '';
}

app.querySelectorAll('[data-speed]').forEach((b) =>
  b.addEventListener('click', () => {
    store.patch({ speed: Number(b.dataset.speed) });
    app.querySelectorAll('[data-speed]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  }),
);

app.querySelectorAll('[data-src]').forEach((b) =>
  b.addEventListener('click', async () => {
    const live = b.dataset.src === 'live';
    // Asked again rather than trusting the check at page load: the usual order on the day is
    // to open the demo first and start the engine second.
    if (live && !(await detectEngine())) {
      toast(
        "No engine answers on 127.0.0.1:8000. In wallet-control-layer, start one — make serve-live for the organizers' sandbox, or make sandbox and make serve offline.",
        'warn',
      );
      return;
    }
    client = live ? createHttpClient() : createMockClient();
    store.patch({ live });
    app.querySelectorAll('[data-src]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    toast(live ? `Switched to the live engine on 127.0.0.1:8000 — ${upstreamLabel}` : 'Replaying recorded engine output');
    await boot();
    // A run this page did not start is still holding the team's queue — typically a reload
    // mid-run. Say so now, rather than let the first order of the next run stall behind it.
    const strangers = live ? await client.strangers?.().catch(() => []) : [];
    if (strangers?.length) {
      toast(`${strangers[0].scenario_id} is still running in the engine from before this page loaded. Let it finish before pressing Run.`, 'warn');
    }
  }),
);

/**
 * Presenter keys. On stage the mouse is the slowest thing in the room:
 *   space  run / pause      →  one order      r  reset      1 2 4  speed
 *   a / d  answer a step-up (approve / decline)
 */
document.addEventListener('keydown', (e) => {
  const t = e.target;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (t instanceof Element && t.matches('input, select, textarea')) return;
  const hit = (sel) => {
    const b = app.querySelector(sel);
    if (b && !b.disabled) b.click();
  };
  switch (e.key) {
    case ' ': e.preventDefault(); hit('[data-run]'); break;
    case 'ArrowRight': e.preventDefault(); hit('[data-step]'); break;
    case 'r': hit('[data-reset]'); break;
    case '1': case '2': case '4': hit(`[data-speed="${e.key}"]`); break;
    case 'a': document.querySelector('.notif [data-yes]')?.click(); break;
    case 'd': document.querySelector('.notif [data-no]')?.click(); break;
    default: break;
  }
});

/* ── rendering ─────────────────────────────────────────────────────────── */

store.subscribe((s) => {
  if (!s.run) return;
  const decided = Object.values(s.counters).reduce((a, b) => a + b, 0) - (s.counters.total ?? 0);
  const total = s.run.total ?? 0;
  const done = s.counters.decided ?? 0;
  q('[data-progress-text]').textContent = `${done} / ${total}`;
  q('[data-progress-fill]').style.width = `${total ? (done / total) * 100 : 0}%`;
  const runBtn = q('[data-run]');
  const runWord = s.running ? 'Pause' : done === 0 || done >= total ? 'Run agent' : 'Resume';
  runBtn.innerHTML = `${s.running ? ico.pause : ico.play}<span class="btn__label">${runWord}</span>`;
  runBtn.disabled = s.revoked;
  q('[data-step]').disabled = s.running || s.revoked || !!awaitingHuman || inFlight;
  void decided;
});

const renderPhone = () => phonePanel.render(store.get());

/* ── boot ──────────────────────────────────────────────────────────────── */

async function boot() {
  const scenarios = await client.listScenarios();
  store.patch({ scenarios });
  const sel = q('[data-scenario]');
  sel.innerHTML = scenarios
    .map((s) => `<option value="${esc(s.id)}">${esc(s.name)} · ${s.event_count} order${s.event_count === 1 ? '' : 's'}</option>`)
    .join('');
  const preferred = scenarios.find((s) => s.id === 'SCEN0004') ?? scenarios[0];
  sel.value = preferred.id;
  await start(preferred.id);
}

async function start(scenarioId) {
  loopToken++;
  awaitingHuman = null;
  decisionsById.clear();
  flagWaiting(false);
  phonePanel.hideStepUp();
  agentPanel.clear();
  leashPanel.empty();

  const scenario = store.get().scenarios.find((s) => s.id === scenarioId);
  q('[data-question]').textContent = scenario?.control_question ?? '';

  const run = await client.createRun(scenarioId);
  const ir = run.mandate?.ir ?? scenario.ir;
  leashPanel.showMandate({ ...scenario, instruction: run.mandate?.instruction ?? scenario.instruction }, ir);
  const cap = Number(ir.rules.find((r) => r.scope === 'purchase')?.value ?? 0);
  const periodRule = ir.rules.find((r) => r.scope === 'period');

  store.patch({
    scenarioId,
    run,
    mandate: { ir, instruction: run.mandate?.instruction ?? scenario.instruction },
    originalCap: cap,
    activeCap: cap,
    counters: { total: run.total, decided: 0 },
    window: periodRule
      ? { period_days: scenario.period_days, approved_spend_chf: '0.00', pending_step_up_chf: '0.00', limit_chf: String(periodRule.value) }
      : null,
    revoked: false,
    running: false,
  });
  phonePanel.render(store.get(), { reset: true });
  // The composed policy and the standing layer, so the mandate card can name
  // which layer set the value in force before the first order lands.
  await refreshPolicy();
}

/* ── the loop ──────────────────────────────────────────────────────────── */

async function run() {
  if (store.get().revoked || awaitingHuman) return;
  while (inFlight) await sleep(60); // let a manual Step finish before taking over
  if (store.get().revoked || awaitingHuman) return;
  store.patch({ running: true });
  const token = ++loopToken;
  while (store.get().running && token === loopToken) {
    const more = await stepOnce(token);
    if (!more) break;
    if (awaitingHuman) break;
    await paced(700);
  }
  if (token === loopToken) store.patch({ running: false });
}

function pause() {
  loopToken++;
  store.patch({ running: false });
}

/** One authorization, end to end. Returns false when the run is done. */
async function stepOnce(token = ++loopToken) {
  const s = store.get();
  if (s.revoked || awaitingHuman || inFlight) return false;
  inFlight = true;
  q('[data-step]').disabled = true;
  try {
    return await evaluateNext(s, token);
  } catch (e) {
    // Live, the platform can refuse — a revoked mandate, an unreachable sandbox. Say what it
    // said and stop, rather than leave the loop marked running with nothing happening.
    toast(`The engine could not continue: ${e.code ?? ''} ${e.message}`.trim(), 'warn');
    store.patch({ running: false });
    return false;
  } finally {
    inFlight = false;
    const now = store.get();
    q('[data-step]').disabled = now.running || now.revoked || !!awaitingHuman;
  }
}

async function evaluateNext(s, token) {
  const event = await client.nextEvent(s.run.run_id);
  if (!event) {
    store.patch({ running: false });
    toast('Scenario complete.', 'ok');
    return false;
  }

  // 1 — the agent works
  agentPanel.openTask(event);
  const steps = agentSteps(event, s.scenarios.find((x) => x.id === s.scenarioId));
  for (const step of steps) {
    if (token !== loopToken && store.get().running) return false;
    agentPanel.pushStep(event.id, step);
    await paced(step.flag === 'authorize' ? 320 : 190);
  }

  // 2 — the leash checks it
  leashPanel.showRequest(event);
  await paced(260);
  const record = await client.decide(s.run.run_id, event.id);
  await leashPanel.runChecks(record, { stepMs: 82 / speedOf() });

  // 3 — the verdict
  leashPanel.showVerdict(record, event);
  decisionsById.set(record.authorization_id, {
    record,
    event,
    finalStatus: record.decision === 'step_up' ? null : record.decision === 'approve' ? 'approved' : 'declined',
  });

  const progress = await client.getRun(s.run.run_id);
  store.patch({ counters: progress.counters, window: progress.window ?? s.window });
  renderPhone();

  const nextEv = null; // the queue is not readable ahead of time by design
  agentPanel.closeTask(event.id, record.decision, agentReaction(record.decision, event, nextEv));

  // 4 — if it needs a human, stop and ask
  if (record.decision === 'step_up') {
    awaitingHuman = { record, event };
    store.patch({ running: false });
    await paced(280);
    flagWaiting(true);
    if (!matchMedia('(min-width: 1180px)').matches) showPanel('you');
    // Live, the window is the platform's: 120 real seconds from when the engine asked,
    // which was before this screen caught up with it. The speed control is ours to bend;
    // the platform's clock is not. Replay keeps the scaled 120 s it was rehearsed with.
    const live = store.get().live && record.step_up?.seconds_remaining != null;
    phonePanel.showStepUp(record, {
      seconds: live ? Math.max(1, Math.floor(record.step_up.seconds_remaining)) : 120,
      tickMs: live ? 1000 : 1000 / speedOf(),
    });
    toast('The agent is waiting for you.', '');
  }
  return true;
}

/* ── human actions ─────────────────────────────────────────────────────── */

async function resolveStepUp(authId, decision, { expired = false } = {}) {
  if (!awaitingHuman || awaitingHuman.record.authorization_id !== authId) return;
  const s = store.get();
  const res = await client.resolveStepUp(s.run.run_id, authId, decision);
  phonePanel.hideStepUp();
  awaitingHuman = null;
  flagWaiting(false);
  if (!matchMedia('(min-width: 1180px)').matches) showPanel('leash');

  const entry = decisionsById.get(authId);
  if (entry) entry.finalStatus = res.status;
  agentPanel.resolveTask(authId, res.status);
  leashPanel.markResolved(res.status);

  const progress = await client.getRun(s.run.run_id);
  store.patch({ counters: progress.counters, window: progress.window ?? s.window });
  renderPhone();

  const closed = expired || res.expired;
  toast(
    closed
      ? 'The 120-second confirmation window closed. Treated as a decline.'
      : `You ${res.status === 'approved' ? 'approved' : 'declined'} it. Recorded as its own audit entry — the original decision is unchanged.`,
    closed ? 'warn' : 'ok',
  );

  await paced(500);
  if (!store.get().revoked) run();
}

/* ── editing settings ──────────────────────────────────────────────────── */

/** Pull the whole editable picture off the client, in either adapter. */
async function refreshPolicy() {
  const s = store.get();
  if (!s.run) return;
  const runId = s.run.run_id;
  const [prefs, candidates] = await Promise.all([
    client.preferences?.() ?? { preferences: {}, rows: [] },
    client.candidates?.(s.scenarioId) ?? null,
  ]);
  const policy = client._internal?.policyOf?.(runId) ?? (await client.policyOf?.(runId)) ?? null;
  const mandateCap = Number(s.mandate?.ir?.rules?.find((r) => r.scope === 'purchase')?.value ?? 0) || null;
  store.patch({
    policy,
    preferences: prefs.preferences ?? {},
    candidates,
    layers: {
      mandateCap,
      preferenceCap: prefs.preferences?.per_order_limit_chf ?? null,
      accountCap: candidates?.account?.per_transaction_limit_chf ?? null,
      effectiveCap: readSetting(policy ?? {}, 'per_order_limit_chf') ?? s.activeCap,
    },
  });
  const effective = store.get().layers.effectiveCap;
  if (effective != null) store.patch({ activeCap: effective });
  renderPhone();
}

/**
 * Open the editor for one setting.
 *
 * `scope` decides which document the edit lands in — the per-errand mandate or
 * the standing preferences layer — and that is the only difference between the
 * two surfaces. One vocabulary, one editor, one classifier.
 */
function openSetting(setting, { scope = 'mandate' } = {}) {
  const s = store.get();
  if (!s.run) return;
  const runId = s.run.run_id;
  const meta = SETTING_META[setting];
  if (!meta) return;

  if (scope === 'preferences') {
    const current = s.preferences?.[setting] ?? (meta.control === 'flag' ? false : null);
    settingsSheet.open({
      setting,
      value: current ?? blankValue(meta),
      origin: `A standing rule. It outlives this errand and seeds the next one.`,
      classify: (value) => classifyPreference(setting, value),
      preview: () => null,
      onApply: async (value) => {
        const next = { ...s.preferences, [setting]: value };
        if (meta.control === 'flag' && !value) delete next[setting];
        await savePreferences(next);
      },
    });
    return;
  }

  const policy = s.policy ?? { hard_rules: s.mandate?.ir?.rules ?? [], intent_facets: s.mandate?.ir?.intent_facets ?? [] };
  const bound = boundRuleFor(policy, setting);
  settingsSheet.open({
    setting,
    value: readSetting(policy, setting) ?? blankValue(meta),
    origin: bound ? `In force now: ${SOURCE_LABEL[sourceOf(bound)]}.` : meta.help,
    // Classified locally so the button can say *before* the round trip whether this
    // costs one tap or a confirmation. The engine classifies authoritatively on apply
    // and its answer wins — `core/policy.js` exists for exactly this, and `check.mjs`
    // asserts the table against the engine's own cases.
    classify: (value) => {
      const base = s.mandate?.ir ?? policy;
      return classify(base, applySettings(base, { [setting]: value }));
    },
    // Replay-only: the engine cannot see the orders still ahead of a live run, because
    // the platform delivers them one at a time. The sheet renders nothing rather than
    // guessing.
    preview: (value) => client._internal?.previewAmend?.(runId, { [setting]: value }) ?? null,
    onApply: (value) => applyAmendment(setting, value),
  });
}

const blankValue = (meta) =>
  meta.control === 'exclusions' ? {}
    : meta.control === 'hours' ? { hours_from: 7, hours_to: 22 }
    : meta.control === 'flag' ? true
    : meta.control === 'money_window' ? { value: 500, period_days: meta.windows[0] }
    : meta.control === 'money' ? 100
    : meta.control === 'choice' ? meta.options[0].value
    : meta.control === 'categories' ? { [meta.key]: [] }
    : { [meta.key]: 1 };

/** Which rule or facet is currently in force for a setting, for the subtitle. */
function boundRuleFor(policy, setting) {
  const meta = SETTING_META[setting];
  if (meta.target === 'rule:purchase') return (policy.hard_rules ?? []).filter((r) => r.scope === 'purchase').sort((a, b) => Number(a.value) - Number(b.value))[0] ?? null;
  if (meta.target === 'rule:period') return (policy.hard_rules ?? []).find((r) => r.scope === 'period') ?? null;
  if (meta.target.startsWith('facet:')) {
    const kind = meta.target.slice('facet:'.length);
    return (policy.intent_facets ?? []).find((f) => f.kind === kind) ?? null;
  }
  return null;
}

/**
 * The standing layer classified against itself: is the customer adding a rule,
 * or relaxing one they set earlier?
 *
 * Either answer is safe for the mandate — a preference can only ever narrow it
 * (invariant I1) — so this labels the button honestly without implying the
 * errand changed.
 */
function classifyPreference(setting, value) {
  const s = store.get();
  const before = { ...s.preferences };
  const after = { ...before, [setting]: value };
  return classify(preferencesToPolicy(before), preferencesToPolicy(after));
}

async function savePreferences(next) {
  try {
    await client.setPreferences(next);
    await refreshPolicy();
    toast('Preferences saved. They apply from the agent\'s next order.', 'ok');
  } catch (e) {
    toast(e.code === 'setting_not_standing'
      ? 'That setting belongs to one errand, not to you — edit it on the mandate.'
      : e.message, 'warn');
  }
}

async function acceptCandidate(candidate) {
  if (!candidate) return;
  const s = store.get();
  const next = {
    ...s.preferences,
    [candidate.setting]: candidate.value,
    origins: { ...(s.preferences.origins ?? {}), [candidate.setting]: { source: 'profile', quote: candidate.quote } },
  };
  await savePreferences(next);
}

/**
 * Apply one edit to the mandate.
 *
 * The engine classifies; we render what it decided. A narrowing lands at once,
 * a widening comes back as a draft that has to be confirmed — and saying which
 * is the whole point of the screen.
 */
async function applyAmendment(setting, value) {
  const s = store.get();
  try {
    const result = await client.amend(s.run.run_id, { [setting]: value });
    if (result.applied) {
      await refreshPolicy();
      toast(
        `${SETTING_META[setting].label} narrowed. It applies from the agent's next order; ` +
          `what you first agreed to stays on the record.`,
        'ok',
      );
      return;
    }
    confirmWidening(result, setting);
  } catch (e) {
    toast(e.code === 'preference_conflict' ? `Refused: ${e.message}` : e.message, 'warn');
  }
}

/**
 * The second tap a widening costs.
 *
 * Not friction for its own sake: the mandate is the customer's consent, and
 * widening it is a new consent moment rather than an edit to an old one.
 */
function confirmWidening(result, setting) {
  const diff = (result.amendment?.changes ?? [])
    .map((c) => `${c.label}: ${c.before ?? 'not set'} → ${c.after ?? 'not set'}`)
    .join(' · ');
  const t = el(html`
    <div class="toast toast--ask">
      <b>This widens your mandate.</b>
      <span>${diff}</span>
      <span class="toast__note">${result.note ?? ''}</span>
      <span class="toast__acts">
        <button class="btn btn--sm btn--ghost" data-no>Not now</button>
        <button class="btn btn--sm btn--brand" data-yes>Confirm</button>
      </span>
    </div>`);
  toastHost.appendChild(t);
  t.querySelector('[data-no]').addEventListener('click', () => t.remove());
  t.querySelector('[data-yes]').addEventListener('click', async () => {
    t.remove();
    try {
      await client.confirmAmendment(store.get().run.run_id);
      await refreshPolicy();
      toast(`Confirmed. ${SETTING_META[setting].label} now reads as you set it.`, 'ok');
    } catch (e) {
      toast(`Not confirmed: ${e.message}`, 'warn');
    }
  });
}

async function tighten(capChf) {
  const s = store.get();
  try {
    const res = await client.tighten(s.run.run_id, capChf);
    store.patch({ activeCap: res.active_cap_chf ?? capChf });
    await refreshPolicy();
    // An order already being evaluated keeps the cap it started under, the way
    // a run keeps its mandate snapshot upstream. Say so, rather than let the
    // next verdict look like the tightening did not take.
    toast(
      `Per-order limit now CHF ${chf(capChf)}, from the next order on. The earlier ` +
        `CHF ${chf(s.originalCap)} stays on the record — the mandate only narrows.`,
      'ok',
    );
  } catch (e) {
    toast(e.code === 'policy_loosened' ? '422 policy_loosened — a mandate can only be tightened.' : e.message, 'warn');
  }
}

async function revoke() {
  const s = store.get();
  pause();
  const res = await client.revoke(s.run.run_id);
  // The revoke is the answer to the question still on the phone: that purchase is declined.
  const declined = res.declined_step_ups ?? [];
  for (const authId of declined) {
    const entry = decisionsById.get(authId);
    if (entry) entry.finalStatus = 'declined';
    agentPanel.resolveTask(authId, 'declined', 'Declined when you revoked. Order not placed.');
  }
  if (awaitingHuman && declined.includes(awaitingHuman.record.authorization_id)) {
    leashPanel.markResolved('declined', 'when you revoked');
  }
  const progress = await client.getRun(s.run.run_id).catch(() => null);
  store.patch({
    revoked: true,
    running: false,
    ...(progress ? { counters: progress.counters, window: progress.window ?? s.window } : {}),
  });
  phonePanel.hideStepUp();
  flagWaiting(false);
  awaitingHuman = null;
  renderPhone();
  agentPanel.halt(`Mandate revoked. ${res.remaining} further order${res.remaining === 1 ? '' : 's'} refused before reaching the control layer — 409 mandate_not_active.`);
  toast(
    declined.length
      ? 'Mandate revoked. The agent is stopped, and the purchase waiting for you was declined.'
      : 'Mandate revoked. The agent is stopped.',
    'warn',
  );
}

/* ── go ────────────────────────────────────────────────────────────────── */

/**
 * Is an engine answering on :8000, and what is it pointed at? Asked at load, again when Live
 * is pressed, and whenever the window regains focus while none has answered — so an engine
 * started after the page opened lights the switch without a reload.
 */
let engineUp = false;
async function detectEngine() {
  const health = await probe();
  engineUp = !!health;
  const btn = app.querySelector('[data-src="live"]');
  if (health) {
    const sandbox = health.upstream?.mode === 'live';
    upstreamLabel = sandbox ? "the organizers' live sandbox" : 'the offline replica';
    btn.removeAttribute('aria-disabled');
    btn.title = `engine ${health.engine_version} · ${upstreamLabel} (${health.upstream?.base_url ?? '?'})`;
    btn.querySelector('.source__full').textContent = sandbox ? 'Live sandbox' : 'Live engine';
  } else {
    btn.setAttribute('aria-disabled', 'true');
    btn.title = 'No engine answers on 127.0.0.1:8000 — press for how to start one';
  }
  return health;
}

window.addEventListener('focus', () => {
  if (!engineUp) detectEngine();
});

(async function main() {
  await boot();
  await detectEngine();
})();
