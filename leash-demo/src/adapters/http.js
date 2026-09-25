/**
 * HttpLeashClient — the same interface, against the real service.
 *
 * Every call maps to an endpoint in `specs/service-contract.md`. The UI never
 * holds the team API key and never talks to the organizers' sandbox: the
 * service owns the key, the Policy IR, the ledger and the audit trail.
 *
 * Start the engine with `make serve` (offline replica) or `make serve-live`
 * (the organizers' sandbox) in wallet-control-layer, then flip the source
 * switch in the header. If the service is not up, the app stays on the mock
 * adapter and says so.
 *
 * ── The run starts when the presenter presses Run, not when a scenario is picked
 *
 * `createRun` creates and confirms the mandate — the customer's consent — and
 * stops there. The platform run starts on the first `nextEvent`. Starting it
 * earlier starts its clock: the service decides at once, and a step-up nobody
 * is looking at yet expires on the platform after 120 s. Until then the run is
 * *staged*, under an id this adapter owns; every method translates it.
 */

const BASE = 'http://127.0.0.1:8000';

async function call(path, { method = 'GET', body, base = BASE } = {}) {
  const res = await fetch(base + path, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const payload = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = new Error(payload?.error?.message ?? res.statusText);
    err.status = res.status;
    err.code = payload?.error?.code;
    throw err;
  }
  return payload;
}

/**
 * A decided authorization, in the shape the panels render.
 *
 * The agent column reconstructs its tool calls from an order's own facts, and the Leash
 * column's request header names the merchant and the amount — so the decision has to carry
 * its subject. `decision_summary` supplies it under `authorization`, and what the engine
 * *derived* — prior orders at this shop, a lookalike, a re-quote, hostile spans — under
 * `context`, in the same shape the replayed fixtures carry.
 */
function toEvent(decision) {
  const a = decision.authorization ?? {};
  const c = decision.context ?? {};
  return {
    id: decision.authorization_id,
    source_authorization_id: decision.source_authorization_id,
    decision: decision.decision,
    reason_codes: decision.reason_codes,
    customer_message: decision.customer_message,
    checks: decision.checks,
    concern_score: decision.score?.concern_score ?? 0,
    latency_ms: decision.timing?.latency_ms ?? 0,
    timestamp: a.timestamp,
    amount: a.amount,
    currency: a.currency,
    billing_amount_chf: a.billing_amount_chf,
    merchant_id: a.merchant_id,
    merchant_name: a.merchant_name,
    merchant_category: a.merchant_category,
    merchant_country: a.merchant_country,
    items: a.items ?? [],
    enrichment: c.enrichment ?? {},
    purchase_description: c.purchase_description ?? '',
    device_id: c.device_id,
    window_before_chf: c.window_before_chf ?? '0.00',
  };
}

export function createHttpClient({ base = BASE } = {}) {
  const scenarios = new Map(); // scenario id → the scenario as listed
  // UI run id → { scenarioId, mandateId, total, runId (null until started), starting }
  const runs = new Map();
  const pendingDraft = new Map(); // UI run id → draft_id, for a widening awaiting consent
  const seen = new Map(); // UI run id → Set of authorization ids already played
  const ready = new Map(); // authorization_id → the decision, fetched once and reused

  /** The service's run id for a UI run id, or null while the run is still staged. */
  const upstreamId = (runId) => (runs.has(runId) ? runs.get(runId).runId : runId);

  /** Start the platform run behind a staged one — once, however many callers ask. */
  function begin(runId) {
    const r = runs.get(runId);
    if (!r) return Promise.resolve(runId);
    if (r.runId) return Promise.resolve(r.runId);
    r.starting ??= call('/v1/runs', {
      method: 'POST',
      body: { scenario_id: r.scenarioId, mandate_id: r.mandateId },
      base,
    }).then((run) => {
      r.runId = run.run_id;
      r.total = run.counters?.total || r.total;
      return r.runId;
    }).finally(() => { r.starting = null; });
    return r.starting;
  }

  return {
    mode: 'live',

    health: () => call('/healthz', { base }),
    config: () => call('/v1/config', { base }),

    /**
     * The scenarios, each with its instruction compiled by the model (`mode=auto`).
     *
     * The live API's instructions carry wording the deterministic baseline was never written
     * for — it read "CHF 250 in any 7-day window" as a per-order cap — so the demo shows and
     * enforces what the model proposed and the guard kept. The service compiles each one once
     * and caches it (`make serve-live` warms the cache at start), so this is instant after the
     * first time, and what the customer reviewed is exactly what `createRun` sends back.
     */
    async listScenarios() {
      const { scenarios: list } = await call('/v1/scenarios?mode=auto', { base });
      for (const s of list) scenarios.set(s.id, s);
      return list;
    },

    compile: (instruction) => call('/v1/mandates/compile', { method: 'POST', body: { instruction, mode: 'auto' }, base }),

    /**
     * Create and confirm the mandate — nothing more. See the header: the platform run
     * starts on the first `nextEvent`.
     *
     * The IR sent is the one `/v1/scenarios` compiled and the mandate card shows, so what
     * the customer reviewed is exactly what is enforced. Leaving it to the service would
     * compile again — a second, unseen IR, since the model does not answer the same way
     * twice, and another pause behind the Live switch.
     */
    async createRun(scenarioId) {
      const scenario = scenarios.get(scenarioId)
        ?? (await this.listScenarios()).find((s) => s.id === scenarioId);
      const draft = await call('/v1/mandates', {
        method: 'POST',
        body: { instruction: scenario.instruction, ir: scenario.ir },
        base,
      });
      const active = await call(`/v1/mandates/${draft.draft_id}/confirm`, {
        method: 'POST',
        body: { confirmed: true },
        base,
      });
      const id = `staged-${active.mandate_id}`;
      runs.set(id, { scenarioId, mandateId: active.mandate_id, total: scenario.event_count, runId: null, starting: null });
      seen.set(id, new Set());
      return {
        run_id: id,
        scenario_id: scenarioId,
        mandate_id: active.mandate_id,
        status: 'ready',
        total: scenario.event_count,
      };
    },

    /**
     * Whether a run is still going on the platform — started, and not yet finished.
     *
     * The real platform serves one queue per team, and a step-up waiting on the customer
     * holds all of it: measured 2026-09-24, a second run's first order sat undelivered
     * behind another run's step-up until its deadline passed and the platform declined it.
     * So a live run finishes, or is revoked, before another starts.
     */
    async busy(runId) {
      const r = runs.get(runId);
      if (!r?.runId) return false;
      const run = await call(`/v1/runs/${r.runId}`, { base }).catch(() => null);
      return run?.status === 'running';
    },

    /** Runs the service is still driving that this page did not start — a reload mid-run. */
    async strangers() {
      const mine = new Set([...runs.values()].map((r) => r.runId));
      const { runs: all } = await call('/v1/runs', { base });
      return all.filter((r) => r.status === 'running' && !mine.has(r.run_id));
    },

    async getRun(runId) {
      const r = runs.get(runId);
      if (r && !r.runId) {
        return { run_id: runId, status: 'ready', counters: { total: r.total, decided: 0 }, window: null, step_ups: [] };
      }
      return call(`/v1/runs/${upstreamId(runId)}`, { base });
    },

    /**
     * The next authorization this run has decided and the UI has not shown yet.
     *
     * The mock hands out events *before* they are judged, because it owns the queue.
     * Here the service owns it: its own loop long-polls upstream, decides, and submits,
     * all without asking us. So the UI cannot be given an undecided event — it polls for
     * a decision that has appeared and reconstructs the request from the facts the
     * decision carries.
     *
     * `null` means the run is over, so this waits for as long as the service still calls
     * the run `running`. On the real platform that can be a while: it generates each
     * order only once the one before is settled, and holds the run at a step-up until
     * the customer answers.
     */
    async nextEvent(runId) {
      const shown = seen.get(runId) ?? new Set();
      seen.set(runId, shown);
      const upstream = await begin(runId);

      for (let attempt = 0; attempt < 720; attempt++) {
        const { decisions } = await call(`/v1/decisions?run_id=${upstream}&limit=200`, { base });
        // `/v1/decisions` is newest-first; the agent worked the other way round.
        const pending = [...decisions].reverse().filter((d) => !shown.has(d.authorization_id));
        if (pending.length) {
          const next = pending[0];
          ready.set(next.authorization_id, next);
          return toEvent(next);
        }
        const run = await call(`/v1/runs/${upstream}`, { base });
        if (run.status !== 'running') return null;
        await new Promise((r) => setTimeout(r, 250));
      }
      return null;
    },

    async decide(runId, authId) {
      const upstream = upstreamId(runId);
      const record = ready.get(authId) ?? null;
      if (record) {
        (seen.get(runId) ?? new Set()).add(authId);
        ready.delete(authId);
        const run = await call(`/v1/runs/${upstream}`, { base });
        // The platform's step-up window runs from the moment the engine asked, not from
        // when this screen reaches the question — the UI replays at human speed behind a
        // service that decides in a millisecond. Carry the real time left.
        const stepUp = (run.step_ups ?? []).find((s) => s.authorization_id === authId) ?? null;
        return { ...record, window: run.window, step_up: stepUp };
      }
      const { decisions } = await call(`/v1/decisions?run_id=${upstream}&limit=200`, { base });
      return decisions.find((d) => d.authorization_id === authId) ?? null;
    },

    /**
     * The customer's answer. An answer that lands after the platform closed the window is
     * refused (409 `authorization_not_pending`), not applied: the platform had already
     * declined the purchase itself. Report that as what happened.
     */
    async resolveStepUp(_runId, authId, decision) {
      try {
        return await call(`/v1/step-ups/${authId}/resolve`, {
          method: 'POST',
          body: { decision, customer_message: 'Confirmed by the cardholder in the app.' },
          base,
        });
      } catch (err) {
        if (err.code !== 'authorization_not_pending') throw err;
        return { authorization_id: authId, status: 'declined', resolved_by: 'timeout', expired: true };
      }
    },

    /**
     * The cap slider. Routes through `/amend` like every other edit, so there
     * is one classifier and the add-only rule list is rebuilt server-side.
     */
    async tighten(runId, capChf) {
      const result = await this.amend(runId, { per_order_limit_chf: capChf });
      if (!result.applied) {
        const err = new Error('policy_loosened');
        err.code = 'policy_loosened';
        err.status = 422;
        throw err;
      }
      return { mandate_id: runs.get(runId)?.mandateId, hard_rules: result.hard_rules, active_cap_chf: capChf };
    },

    /**
     * POST /v1/mandates/{id}/amend — the one entry point for an edit.
     *
     * The engine classifies: a narrowing applies immediately and add-only, a
     * widening comes back as a draft that changes nothing until it is
     * confirmed. The UI renders what came back rather than deciding itself.
     */
    async amend(runId, settings) {
      const mandateId = runs.get(runId)?.mandateId;
      const result = await call(`/v1/mandates/${mandateId}/amend`, {
        method: 'POST',
        body: { settings },
        base,
      });
      if (result.draft_id) pendingDraft.set(runId, result.draft_id);
      return result;
    },

    /** The second tap a widening costs: the new mandate's own confirmation. */
    async confirmAmendment(runId) {
      const draftId = pendingDraft.get(runId);
      if (!draftId) throw new Error('nothing awaiting confirmation');
      const confirmed = await call(`/v1/mandates/${draftId}/confirm`, {
        method: 'POST',
        body: { confirmed: true },
        base,
      });
      pendingDraft.delete(runId);
      // A run keeps the mandate snapshot it started with, so the widened one
      // binds from the next run — unless this one is still staged, in which case
      // it is the mandate this run will start under.
      const r = runs.get(runId);
      if (r) r.mandateId = confirmed.mandate_id;
      return confirmed;
    },

    /* ── the standing preferences layer ─────────────────────────────────── */

    preferences: () => call('/v1/preferences', { base }),

    setPreferences: (next) => call('/v1/preferences', { method: 'PUT', body: next, base }),

    candidates: (scenarioId) =>
      call(`/v1/preferences/candidates?scenario_id=${encodeURIComponent(scenarioId)}`, { base }),

    /**
     * The composed policy in force for a run.
     *
     * The service owns the composition, so this reads the mandate's IR and lets
     * the engine's own `/v1/decide` be the authority on what it means. The UI
     * uses it only to label rows with the layer that set them.
     */
    async policyOf(runId) {
      const mandateId = runs.get(runId)?.mandateId;
      if (!mandateId) return null;
      const mandate = await call(`/v1/mandates/${mandateId}`, { base });
      const ir = mandate.ir ?? {};
      return {
        hard_rules: ir.rules ?? mandate.hard_rules ?? [],
        intent_facets: ir.intent_facets ?? [],
        uncertainty_policy: ir.uncertainty_policy ?? 'ask',
      };
    },

    /**
     * Withdraw the permission. The service stops the run bound to the mandate and declines
     * a step-up still waiting on the customer before it revokes — `declined_step_ups` says
     * which. `remaining` is what the agent had not yet placed: every order of a run that
     * never started.
     *
     * The run is enforced under the mandate it started with; a widening confirmed since binds
     * only a later run. So that one is revoked first — and the widening with it, if any.
     */
    async revoke(runId) {
      const r = runs.get(runId);
      const before = r?.runId ? await call(`/v1/runs/${r.runId}`, { base }).catch(() => null) : null;
      const bound = before?.mandate_id ?? r?.mandateId;
      const res = await call(`/v1/mandates/${bound}`, { method: 'DELETE', base });
      if (r?.mandateId && r.mandateId !== bound) {
        await call(`/v1/mandates/${r.mandateId}`, { method: 'DELETE', base }).catch(() => null);
      }
      const after = r?.runId ? await call(`/v1/runs/${r.runId}`, { base }).catch(() => null) : null;
      const remaining = Math.max(0, (r?.total ?? 0) - (after?.counters?.decided ?? 0));
      return { ...res, remaining };
    },

    audit: (authId) => call(`/v1/audit/${authId}`, { base }),
  };
}

/** Probe for a live service; used by the source switch in the header. */
export async function probe(base = BASE) {
  try {
    const res = await fetch(base + '/healthz', { signal: AbortSignal.timeout(1200) });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}
