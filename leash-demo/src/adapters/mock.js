/**
 * MockLeashClient — implements the LeashClient interface with no server.
 *
 * It replays `src/data/engine-output.js`: 45 authorization events decided by the
 * real Wallet Control Layer over the organizers' data pack. Verdicts, evidence,
 * concern scores, latencies and customer messages are the engine's own output
 * and are never recomputed here — **until the customer edits a setting.**
 *
 * Three things this adapter *does* compute, because they are properties of the
 * demo session rather than of a fixture:
 *
 *   1. the rolling-window ledger as approvals and resolved step-ups land,
 *   2. the composed policy — account ceiling, standing preferences, mandate —
 *      tightest-wins, and
 *   3. the re-decision after the cardholder edits any of it.
 *
 * (2) and (3) reimplement rules the engine owns: `decision-rules.md` §1.1 and
 * §9 (the tightest cap of a scope binds), `customer-settings.md` §3 and §6
 * (composition and the tighten/widen classifier), and the editable checks
 * themselves in `recheck.js`. All of it is recomposed with the engine's own
 * grammar (see MESSAGE GRAMMAR below) and asserted by `check.mjs`. Swapping in
 * HttpClient sends a real `POST /v1/mandates/{id}/amend` and re-decides through
 * `POST /v1/decide` instead; the UI cannot tell the difference.
 *
 * **A run that nobody has edited is never recomputed.** `run.edited` gates the
 * whole path, so the 45 fixtures replay byte-identically until the moment the
 * customer actually changes something.
 */

import { ENGINE_OUTPUT } from '../data/engine-output.js';
import { candidatesFor } from '../data/profiles.js';
import { recheck } from './recheck.js';
import {
  applySettings,
  bindingCap,
  classify,
  compose,
  preferencesToPolicy,
} from '../core/policy.js';
import { STANDING_SETTINGS, SETTING_META } from '../core/client.js';
import { preferenceSentence } from '../core/policy.js';

const clone = (v) => JSON.parse(JSON.stringify(v));
const n = (v) => Number(v ?? 0);
const f2 = (v) => n(v).toFixed(2);

/* ── MESSAGE GRAMMAR ────────────────────────────────────────────────────────
   Reproduced from specs/customer-message.md, verified against all 45 fixtures:

     approve  "Approved: {amount} at {merchant}. {pass details; joined}."
     decline  "Declined: {amount} at {merchant}. {violation details; joined}.
               {follow-ups}[ Also noticed, and it did not change this decision:
               {concern details}.]"
     step_up  "Needs your confirmation: {amount} at {merchant}. {Concern
               details}. Approve or decline in the app."

   {amount} is "CHF 391.50 (USD 450.00)" when the row currency is not CHF.
   A reason code never appears in a customer message.                        */

function amountPhrase(ev) {
  const chf = `CHF ${f2(ev.billing_amount_chf)}`;
  return ev.currency && ev.currency !== 'CHF' ? `${chf} (${ev.currency} ${f2(ev.amount)})` : chf;
}

const cap1 = (s) => (s ? s[0].toUpperCase() + s.slice(1) : '');

function composeMessage(ev, checks, decision) {
  const withDetail = (v) => checks.filter((c) => c.verdict === v && c.detail);
  const head = `${amountPhrase(ev)} at ${ev.merchant_name}`;

  if (decision === 'decline') {
    const violations = withDetail('violation');
    const follow = violations.map((c) => c.follow_up).filter(Boolean);
    const concerns = [...withDetail('concern'), ...withDetail('unknown')];
    // `explain()` runs every joined cause through `_sentence`, which capitalises the opening
    // and terminates it. Every decline detail in the fixtures happens to start with "CHF" or
    // a shop name, so this was invisible until a check whose detail starts with a pronoun —
    // "this order contains…", "it was placed at…" — landed here.
    let msg = `Declined: ${head}. ${cap1(violations.map((c) => c.detail).join('; '))}.`;
    if (follow.length) msg += ` ${follow.join(' ')}`;
    if (concerns.length)
      msg += ` Also noticed, and it did not change this decision: ${concerns.map((c) => c.detail).join('; ')}.`;
    return msg;
  }
  if (decision === 'step_up') {
    const raised = [...withDetail('concern'), ...withDetail('unknown')];
    return `Needs your confirmation: ${head}. ${cap1(raised.map((c) => c.detail).join('; '))}. Approve or decline in the app.`;
  }
  const passes = withDetail('pass');
  return `Approved: ${head}. ${cap1(passes.map((c) => c.detail).join('; '))}.`;
}

/**
 * decision-rules.md §5 — the combination, in order.
 *
 * A definite breach of an instruction the customer wrote outranks a soft risk
 * signal, and an unestablished fact is never resolved silently in the agent's
 * favour. The `unknown` branch is what makes `uncertainty_policy` an editable
 * setting rather than a label: change it and the same evidence set produces a
 * different decision.
 */
function decisionAlgebra(checks, concernScore, threshold, uncertaintyPolicy = 'ask') {
  if (checks.some((c) => c.verdict === 'violation')) return 'decline';
  if (checks.some((c) => c.verdict === 'unknown')) {
    return { ask: 'step_up', decline: 'decline', approve: 'approve' }[uncertaintyPolicy] ?? 'step_up';
  }
  if (concernScore >= threshold) return 'step_up';
  return 'approve';
}

/** The weighted sum of the concerns still standing after a re-decision. */
function concernScore(checks) {
  return checks
    .filter((c) => c.verdict === 'concern')
    .reduce((total, c) => total + Number(c.weight ?? 0), 0);
}


/* ── the client ────────────────────────────────────────────────────────── */

export function createMockClient({ latency = 0 } = {}) {
  const data = clone(ENGINE_OUTPUT);
  const threshold = data.config.step_up_threshold;
  const runs = new Map();
  let seq = 0;

  // The standing preferences layer. One customer, held for the session — in the
  // product it is customer data in the one app, which the engine reads.
  let preferences = {};

  const wait = () => (latency ? new Promise((r) => setTimeout(r, latency)) : Promise.resolve());

  const scenarioOf = (id) => data.scenarios.find((s) => s.id === id);

  /* ── the three layers ─────────────────────────────────────────────────── */

  /**
   * Account ceiling, standing preferences, mandate — composed tightest-wins.
   * `customer-settings.md` §3. The account limit is a real number from the pack
   * (CHF 900–1400 across the five cardholders) and never binds against these
   * instructions, which is exactly why it is safe to layer: it proves the
   * mechanism without moving the board.
   */
  function effectivePolicy(run) {
    const account = candidatesFor(run.scenarioId).account;
    return compose(
      {
        source: 'account',
        policy: account
          ? {
              hard_rules: [
                {
                  field: 'billing_amount_chf',
                  operator: '<=',
                  value: account.per_transaction_limit_chf,
                  currency: 'CHF',
                  scope: 'purchase',
                  provenance: [{ source: 'account', quote: account.quote }],
                },
              ],
            }
          : {},
      },
      { source: 'preferences', policy: preferencesToPolicy(preferences) },
      { source: 'mandate', policy: run.ir },
    ).policy;
  }

  /** The per-order cap currently in force, across every layer. */
  const activeCap = (run) => {
    const rule = bindingCap(effectivePolicy(run).hard_rules, 'purchase');
    return rule ? n(rule.value) : null;
  };

  /**
   * Re-decide one fixture under the run's current policy.
   *
   * Returns the engine's own record **untouched** while nothing has been
   * edited, which is what keeps the 45-decision replay byte-identical.
   */
  function decideUnderPolicy(run, ev) {
    const record = {
      authorization_id: ev.id,
      decision: ev.decision,
      reason_codes: [...ev.reason_codes],
      customer_message: ev.customer_message,
      checks: clone(ev.checks),
      // `...ev.trust` carries the engine's own trust score and its arithmetic
      // (specs/trust-score.md). It is dropped below when an edit re-decides the event,
      // because a score for the old decision beside a new verdict would be a lie — and this
      // adapter cannot recompute it without reimplementing the scale.
      score: { concern_score: ev.concern_score, step_up_threshold: threshold, ...ev.trust },
      timing: { latency_ms: ev.latency_ms },
      tightened: false,
    };
    if (!run.edited) return record;

    const policy = effectivePolicy(run);
    record.checks = recheck(ev, record.checks, policy);
    const score = concernScore(record.checks);
    // The customer may have set how much evidence it takes to interrupt them.
    const bar = Number(policy.step_up_threshold ?? threshold);
    const next = decisionAlgebra(record.checks, score, bar, policy.uncertainty_policy);

    record.score.concern_score = score;
    record.score.step_up_threshold = bar;
    if (
      next !== ev.decision ||
      JSON.stringify(record.checks) !== JSON.stringify(ev.checks)
    ) {
      record.decision = next;
      record.customer_message = composeMessage(ev, record.checks, next);
      // The recorded score belonged to the recorded decision. Live mode gets a fresh one
      // from the engine; here the honest answer is to show none.
      record.score = { concern_score: score, step_up_threshold: bar };
      record.reason_codes = record.checks
        .filter((c) => c.reason_code && c.verdict !== 'not_applicable')
        .filter((c) => (next === 'approve' ? c.verdict === 'pass' : c.verdict !== 'pass'))
        .map((c) => c.reason_code);
      record.tightened = next !== ev.decision;
    }
    return record;
  }

  /* ── ledger ───────────────────────────────────────────────────────────── */

  function recomputeWindow(run) {
    // The window ROLLS: approvals older than the period age out again. A
    // counter that just adds up this run's approvals reads 299.50 at AU0011
    // and declines a legitimate grocery delivery; the true figure is 223.00.
    // So the ledger is read from the engine's own `window_before_chf` stamped
    // on the most recent event, plus that event's amount if it was approved —
    // never accumulated here. (decision-rules.md §2.1)
    const last = run.decisions[run.decisions.length - 1];
    const base = last ? n(last.window_before_chf) : run.windowBase;
    const own = last && last.finalStatus === 'approved' ? n(last.billing_amount_chf) : 0;
    const pending = run.decisions
      .filter((d) => d.decision === 'step_up' && !d.finalStatus)
      .reduce((a, d) => a + n(d.billing_amount_chf), 0);
    run.window = {
      period_days: run.periodDays,
      approved_spend_chf: f2(base + own),
      pending_step_up_chf: f2(pending),
      limit_chf: run.periodLimit == null ? null : f2(run.periodLimit),
    };
  }

  /* ── API surface ──────────────────────────────────────────────────────── */

  return {
    mode: 'mock',

    async health() {
      await wait();
      return {
        status: 'ok',
        engine_version: 'leash-0.1.0',
        upstream: { base_url: 'replay://data-pack', reachable: true, mode: 'replay' },
        checks_registered: data.config.checks.length,
      };
    },

    async config() {
      await wait();
      return data.config;
    },

    async listScenarios() {
      await wait();
      return data.scenarios.map((s) => ({
        id: s.id,
        name: s.name,
        instruction: s.instruction,
        control_question: s.control_question,
        theme: s.theme,
        period_days: s.period_days,
        event_count: s.events.length,
        ir: s.ir,
      }));
    },

    /** POST /v1/mandates/compile — here, the IR the engine already compiled. */
    async compile(scenarioId) {
      await wait();
      const s = scenarioOf(scenarioId);
      return { ...clone(s.ir), instruction: s.instruction, compiler: s.ir.compiler };
    },

    /** POST /v1/runs */
    async createRun(scenarioId) {
      await wait();
      const s = scenarioOf(scenarioId);
      const runId = `RUN_${String(++seq).padStart(3, '0')}`;
      const periodRule = s.ir.rules.find((r) => r.scope === 'period');
      const run = {
        run_id: runId,
        scenarioId,
        status: 'running',
        queue: clone(s.events),
        cursor: 0,
        decisions: [],
        // The mandate as confirmed. Amendments rewrite this; the preferences and
        // account layers sit outside it and are composed at decision time.
        ir: clone(s.ir),
        originalIr: clone(s.ir),
        edited: false,
        periodDays: s.period_days,
        periodLimit: periodRule ? n(periodRule.value) : null,
        windowBase: n(s.events[0]?.window_before_chf ?? 0),
        window: null,
      };
      recomputeWindow(run);
      runs.set(runId, run);
      return {
        run_id: runId,
        scenario_id: scenarioId,
        status: 'running',
        total: run.queue.length,
        mandate: { ir: clone(s.ir), instruction: s.instruction },
      };
    },

    async getRun(runId) {
      await wait();
      const run = runs.get(runId);
      if (!run) throw new Error('unknown run');
      const counters = { total: run.queue.length, decided: run.decisions.length };
      for (const d of run.decisions) counters[d.decision] = (counters[d.decision] ?? 0) + 1;
      // What the cardholder sees is the *outcome*, and the three states are
      // mutually exclusive: a step-up they answered is no longer waiting.
      counters.final = {
        approved: run.decisions.filter((d) => d.finalStatus === 'approved').length,
        declined: run.decisions.filter((d) => d.finalStatus === 'declined').length,
        waiting: run.decisions.filter((d) => d.decision === 'step_up' && !d.finalStatus).length,
      };
      return { run_id: runId, status: run.status, counters, window: run.window, decisions: run.decisions };
    },

    /** The long-poll: the next authorization.request the platform delivers. */
    async nextEvent(runId) {
      await wait();
      const run = runs.get(runId);
      if (!run || run.status !== 'running') return null;
      if (run.cursor >= run.queue.length) {
        run.status = 'complete';
        return null;
      }
      return clone(run.queue[run.cursor++]);
    },

    /** POST /v1/decide */
    async decide(runId, authId) {
      await wait();
      const run = runs.get(runId);
      const ev = run.queue.find((e) => e.id === authId);
      const record = decideUnderPolicy(run, ev);
      const entry = {
        ...record,
        billing_amount_chf: ev.billing_amount_chf,
        window_before_chf: ev.window_before_chf,
        merchant_name: ev.merchant_name,
        timestamp: ev.timestamp,
        finalStatus: record.decision === 'approve' ? 'approved' : record.decision === 'decline' ? 'declined' : null,
        expires_at: record.decision === 'step_up' ? Date.now() + 120_000 : null,
      };
      run.decisions.push(entry);
      recomputeWindow(run);
      return { ...record, window: run.window };
    },

    /** POST /v1/step-ups/{id}/resolve */
    async resolveStepUp(runId, authId, decision) {
      await wait();
      const run = runs.get(runId);
      const entry = run.decisions.find((d) => d.authorization_id === authId);
      if (!entry) throw new Error('unknown authorization');
      if (entry.finalStatus) throw new Error('already_resolved');
      entry.finalStatus = decision === 'approve' ? 'approved' : 'declined';
      entry.resolved_by = 'customer';
      recomputeWindow(run);
      return { authorization_id: authId, status: entry.finalStatus, resolved_by: 'customer', window: run.window };
    },

    /**
     * PATCH /v1/mandates/{id} — tighten-only, the per-order cap.
     * Kept as its own method because the slider is the demo's fastest gesture;
     * it routes through `amend` so there is one classifier, not two.
     */
    async tighten(runId, capChf) {
      const result = await this.amend(runId, { per_order_limit_chf: capChf });
      if (!result.applied) {
        const err = new Error('policy_loosened');
        err.code = 'policy_loosened';
        err.status = 422;
        throw err;
      }
      return { mandate_id: `TM_${runId}`, hard_rules: clone(result.ir.rules), active_cap_chf: activeCap(runs.get(runId)) };
    },

    /**
     * POST /v1/mandates/{id}/amend — the one entry point for an edit.
     *
     * The caller does not say whether this narrows or widens; the classifier
     * decides. A narrowing applies immediately and **add-only**, so what the
     * customer first agreed to stays on the record and §9 makes it inert. A
     * widening returns a draft and changes nothing until it is confirmed.
     */
    async amend(runId, settings) {
      await wait();
      const run = runs.get(runId);
      if (!run) throw new Error('unknown run');

      const proposed = applySettings(run.ir, settings);
      const move = classify(run.ir, proposed);

      if (move.kind === 'conflict') {
        const err = new Error(move.changes.find((c) => c.kind === 'conflict')?.why ?? 'conflict');
        err.code = 'preference_conflict';
        err.status = 422;
        throw err;
      }
      if (!move.appliesImmediately) {
        run.pendingDraft = proposed;
        return {
          kind: move.kind,
          applied: false,
          awaiting: 'confirm',
          draft_id: `TM_${runId}_DRAFT`,
          ir: proposed,
          amendment: move,
          note: 'confirm this to make it enforceable; it binds from the next order',
        };
      }
      // Add-only: the narrower rule is appended beside the one it supersedes.
      const kept = run.ir.rules ?? [];
      const added = (proposed.rules ?? []).filter((r) => !kept.some((k) => JSON.stringify(k) === JSON.stringify(r)));
      run.ir = { ...proposed, rules: [...kept, ...added], hard_rules: [...kept, ...added] };
      run.edited = true;
      return { kind: move.kind, applied: true, ir: run.ir, amendment: move, active_cap_chf: activeCap(run) };
    },

    /**
     * The customer confirmed a widening. In the product this is a fresh mandate
     * the platform snapshots at the next run; here the run adopts it from the
     * next order on, and the UI says which.
     */
    async confirmAmendment(runId) {
      await wait();
      const run = runs.get(runId);
      if (!run?.pendingDraft) throw new Error('nothing awaiting confirmation');
      run.ir = run.pendingDraft;
      run.pendingDraft = null;
      run.edited = true;
      return { mandate_id: `TM_${runId}_V2`, status: 'active', ir: run.ir, active_cap_chf: activeCap(run) };
    },

    /* ── the standing layer ─────────────────────────────────────────────── */

    async preferences() {
      await wait();
      return { preferences: clone(preferences), rows: describePreferences(preferences) };
    },

    /**
     * PUT /v1/preferences. Refuses every setting no check reads, and says which
     * — an inert control is worse than a missing one, because the customer
     * believes they are protected by it.
     */
    async setPreferences(next) {
      await wait();
      const offending = Object.keys(next ?? {})
        .filter((k) => k !== 'origins')
        .filter((k) => !STANDING_SETTINGS.includes(k));
      if (offending.length) {
        const err = new Error(
          offending
            .map((k) =>
              SETTING_META[k]
                ? `${k} belongs to one errand, not to you`
                : `no check reads '${k}', so it cannot be enforced`,
            )
            .join('; '),
        );
        err.code = SETTING_META[offending[0]] ? 'setting_not_standing' : 'unknown_setting';
        err.status = 422;
        throw err;
      }
      const before = preferencesToPolicy(preferences);
      preferences = clone(next ?? {});
      // Every layer can only narrow, so a live run picks this up from its next
      // order rather than waiting for the next errand.
      for (const run of runs.values()) run.edited = true;
      return {
        preferences: clone(preferences),
        rows: describePreferences(preferences),
        amendment: classify(before, preferencesToPolicy(preferences)),
      };
    },

    /** GET /v1/preferences/candidates — proposals a human accepts, never rules. */
    async candidates(scenarioId) {
      await wait();
      return candidatesFor(scenarioId);
    },

    /** DELETE /v1/mandates/{id} */
    /**
     * DELETE /v1/mandates/{id}. The revoke is the customer's answer to every question the
     * mandate still has open: a step-up waiting on them is declined, not left hanging —
     * live, a pending one would hold the platform's whole queue with nobody left to answer.
     */
    async revoke(runId) {
      await wait();
      const run = runs.get(runId);
      run.status = 'revoked';
      const declined = run.decisions.filter((d) => d.decision === 'step_up' && !d.finalStatus);
      for (const d of declined) {
        d.finalStatus = 'declined';
        d.resolved_by = 'revocation';
      }
      if (declined.length) recomputeWindow(run);
      return {
        status: 'revoked',
        stopped_at: run.cursor,
        remaining: run.queue.length - run.cursor,
        declined_step_ups: declined.map((d) => d.authorization_id),
      };
    },

    /** Demo-only affordances the HTTP client fulfils differently. */
    _internal: {
      run: (id) => runs.get(id),
      scenario: scenarioOf,
      eventOf: (runId, authId) => runs.get(runId)?.queue.find((e) => e.id === authId),
      policyOf: (runId) => {
        const run = runs.get(runId);
        return run ? effectivePolicy(run) : null;
      },
      previewCap: (runId, cap) => previewAmend(runId, { per_order_limit_chf: cap }).flips,
      /**
       * What would this edit do to the orders still ahead?
       *
       * The single most convincing thing in the editor: a policy screen that
       * shows consequences before you commit is a different product from one
       * that does not. Nothing is mutated — the run's own IR is restored.
       */
      previewAmend: (runId, settings) => previewAmend(runId, settings),
      classify: (runId, settings) => {
        const run = runs.get(runId);
        if (!run) return { kind: 'noop', changes: [] };
        return classify(run.ir, applySettings(run.ir, settings));
      },
      // The standing layer classified against itself: is the customer adding a
      // rule, or relaxing one they set earlier? Either way it cannot widen a
      // mandate — the mandate's own rules still bind — so this labels the
      // button honestly without implying the errand changed.
      classifyPreferences: (before, after) =>
        classify(preferencesToPolicy(before), preferencesToPolicy(after)),
    },
  };

  function previewAmend(runId, settings) {
    const run = runs.get(runId);
    if (!run) return { flips: 0, changes: [] };
    const savedIr = run.ir;
    const savedEdited = run.edited;
    try {
      run.ir = applySettings(run.ir, settings);
      run.edited = true;
      const ahead = run.queue.slice(run.cursor);
      const changed = ahead.filter((ev) => decideUnderPolicy(run, ev).decision !== ev.decision);
      return {
        flips: changed.length,
        declines: changed.filter((ev) => decideUnderPolicy(run, ev).decision === 'decline').length,
        ahead: ahead.length,
      };
    } finally {
      run.ir = savedIr;
      run.edited = savedEdited;
    }
  }

  function describePreferences(prefs) {
    return STANDING_SETTINGS.filter((key) => prefs?.[key] != null && prefs[key] !== false).map((key) => ({
      setting: key,
      label: SETTING_META[key].label,
      value: prefs[key],
      sentence: preferenceSentence(key, prefs),
      source: prefs.origins?.[key]?.source ?? 'preferences',
      quote: prefs.origins?.[key]?.quote ?? '',
    }));
  }
}
