#!/usr/bin/env node
/**
 * Does this demo still tell the truth?
 *
 *   node check.mjs
 *
 * Asserts the mock adapter against the engine's own regression baseline and
 * against the two claims the demo makes on stage that are computed here rather
 * than replayed: the rolling window, and the re-decision after a tighten.
 * If the engine changes and `src/data/engine-output.js` is regenerated, this is
 * what tells you whether the demo's narrative still holds.
 */

import { readFileSync } from 'node:fs';

import { createMockClient } from './src/adapters/mock.js';
import { ENGINE_OUTPUT } from './src/data/engine-output.js';
import { agentSteps } from './src/data/agent-script.js';
import { SETTING_META, TASK_ONLY_FACETS } from './src/core/client.js';
import { classify, compose, periodCaps, preferencesToPolicy } from './src/core/policy.js';
import { PROFILES, candidatesFor } from './src/data/profiles.js';

/** Minimal CSV row parser — the pack quotes fields containing commas. */
function parseRow(line) {
  const out = [];
  let cur = '';
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cur += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') { out.push(cur); cur = ''; }
    else cur += ch;
  }
  out.push(cur);
  return out;
}

let failures = 0;
const ok = (cond, label, detail = '') => {
  const mark = cond ? '  ok  ' : ' FAIL ';
  if (!cond) failures++;
  console.log(`${mark} ${label}${detail ? `\n        ${detail}` : ''}`);
};

/* 1 — the board matches the engine's regression baseline ─────────────── */

const client = createMockClient();
const scenarios = await client.listScenarios();
const board = { approve: 0, decline: 0, step_up: 0 };
let total = 0;

for (const s of scenarios) {
  const run = await client.createRun(s.id);
  let ev;
  while ((ev = await client.nextEvent(run.run_id))) {
    const d = await client.decide(run.run_id, ev.id);
    board[d.decision]++;
    total++;
    // the replay must not alter the engine's own record
    if (d.customer_message !== ev.customer_message) {
      ok(false, `${ev.id} message preserved`, `got: ${d.customer_message}`);
    }
  }
}

console.log('\n— the board —');
ok(total === 45, `45 authorizations replayed`, `got ${total}`);
ok(
  board.approve === 17 && board.decline === 20 && board.step_up === 8,
  'board is 17 approve · 20 decline · 8 step-up',
  `got ${board.approve} / ${board.decline} / ${board.step_up}`,
);

/* 2 — every rule quotes the cardholder verbatim ──────────────────────── */

console.log('\n— provenance —');
for (const s of scenarios) {
  const spans = [...s.ir.rules, ...(s.ir.intent_facets ?? [])].map((r) => r.provenance);
  const missing = spans.filter((p) => !s.instruction.includes(p));
  ok(missing.length === 0, `${s.id} — ${spans.length} span(s) found verbatim`, missing.join(' | '));
}

/* 3 — the window rolls; it is not a cumulative counter ───────────────── */

console.log('\n— the rolling window —');
{
  const run = await client.createRun('SCEN0001');
  const ledger = [];
  let ev;
  while ((ev = await client.nextEvent(run.run_id))) {
    const d = await client.decide(run.run_id, ev.id);
    ledger.push([ev.id, d.window.approved_spend_chf]);
  }
  const at = (id) => ledger.find((l) => l[0] === id)?.[1];
  ok(at('AU0008') === '300.00', 'AU0008 lands on exactly 300.00 and is approved — the cap is `<=`', `got ${at('AU0008')}`);
  ok(at('AU0010') === '255.50', 'AU0010 has fallen back to 255.50 — orders aged out', `got ${at('AU0010')}`);
  ok(at('AU0011') === '223.50', 'AU0011 reads 223.50, not the cumulative 388.00', `got ${at('AU0011')}`);
}

/* 3c — the order split in two ────────────────────────────────────────── */

console.log('\n— the split order —');
{
  const run = await client.createRun('SCEN0001');
  const seen = {};
  let ev;
  while ((ev = await client.nextEvent(run.run_id))) {
    seen[ev.id] = await client.decide(run.run_id, ev.id);
  }
  ok(seen.AU0005?.decision === 'approve', 'AU0005 (CHF 70.00, 17:20) is an ordinary order');
  ok(
    seen.AU0006?.decision === 'step_up' && seen.AU0006.reason_codes.includes('split_order_suspected'),
    'AU0006 (CHF 65.00, 17:26) is asked — CHF 135.00 against a CHF 120 cap',
    `got ${seen.AU0006?.decision} ${seen.AU0006?.reason_codes?.join(',')}`,
  );
  ok(
    seen.AU0006?.customer_message?.includes('Each order is within your limit on its own'),
    'and says why it is a question rather than a refusal',
    seen.AU0006?.customer_message,
  );
  ok(
    seen.AU0008?.decision === 'approve',
    'AU0008 is approved — the CHF 65.00 never entered the ledger',
    `got ${seen.AU0008?.decision}`,
  );
}

/* 3b — the ring: what the arc is asked to draw, order by order ───────── */

console.log('\n— the spend ring —');
{
  const C = 2 * Math.PI * 30;
  const run = await client.createRun('SCEN0001');
  const frames = [];
  let lastUsed = null; // mirrors phone-panel.js ledgerFigures()
  let ev;
  while ((ev = await client.nextEvent(run.run_id))) {
    const d = await client.decide(run.run_id, ev.id);
    const used = Number(d.window.approved_spend_chf);
    const limit = Number(d.window.limit_chf);
    const freed = lastUsed != null && used < lastUsed - 0.005 ? lastUsed - used : 0;
    lastUsed = used;
    const pct = Math.min(1, used / limit);
    frames.push({ id: ev.id, used, pct, offset: C * (1 - pct), freed });
  }

  const moved = frames.filter((f, i) => i === 0 || Math.abs(f.offset - frames[i - 1].offset) > 0.01);
  ok(moved.length >= 5, `the arc moves on ${moved.length} of ${frames.length} orders`);

  const backwards = frames.filter((f) => f.freed > 0);
  ok(
    backwards.length === 2,
    'the arc runs backwards twice, as earlier orders age out',
    backwards.map((f) => `${f.id} freed CHF ${f.freed.toFixed(2)}`).join(' · '),
  );
  ok(
    backwards[0]?.freed.toFixed(2) === '44.50' && backwards[1]?.freed.toFixed(2) === '32.00',
    'and frees CHF 44.50 then CHF 32.00',
    backwards.map((f) => f.freed.toFixed(2)).join(' / '),
  );

  const peak = frames.reduce((a, f) => (f.pct > a.pct ? f : a));
  ok(Math.round(peak.pct * 100) === 100, 'the ring reaches 100% before it rolls back', `peak ${Math.round(peak.pct * 100)}% at ${peak.id}`);
  ok(
    frames.every((f) => f.offset >= -0.01 && f.offset <= C + 0.01),
    'every offset stays inside the circumference',
  );
}

/* 4 — the mandate only ever narrows ──────────────────────────────────── */

console.log('\n— tighten-only —');
{
  const run = await client.createRun('SCEN0004');
  await client.tighten(run.run_id, 250);
  let loosened = null;
  try {
    await client.tighten(run.run_id, 900);
  } catch (e) {
    loosened = e.code;
  }
  ok(loosened === 'policy_loosened', 'widening a cap is refused (422 policy_loosened)', `got ${loosened}`);

  let ev;
  let au0038 = null;
  while ((ev = await client.nextEvent(run.run_id))) {
    const d = await client.decide(run.run_id, ev.id);
    if (ev.id === 'AU0038') au0038 = d;
  }
  // The cap that bound is no longer the one in the instruction, so the message names where
  // it came from — otherwise the customer reads their own sentence, sees CHF 400, and
  // concludes the engine is wrong. Verified word for word against the engine on 2026-09-21:
  //   POST /v1/mandates/{id}/amend {"per_order_limit_chf": 250} then POST /v1/decide
  const expected =
    'Declined: CHF 391.50 (USD 450.00) at HarborByte. CHF 391.50 exceeds the CHF 250.00 ' +
    'per-order limit you set in the app. An order of CHF 250.00 or less would be within your limit.';
  ok(au0038?.decision === 'decline', 'AU0038 flips approve → decline at a CHF 250 cap');
  ok(au0038?.customer_message === expected, 'and says so in the engine\'s own grammar', au0038?.customer_message);
}

/* 4a — a revoke answers the question still open ─────────────────────── */

console.log('\n— revoke —');
{
  // The service declines a step-up still waiting on the customer when the mandate is revoked
  // (service-contract.md, DELETE /v1/mandates): live, a pending one would hold the platform's
  // whole queue with nobody left to answer it. The mock must tell the same story.
  const run = await client.createRun('SCEN0004');
  let ev;
  let asked = null;
  while (!asked && (ev = await client.nextEvent(run.run_id))) {
    const d = await client.decide(run.run_id, ev.id);
    if (d.decision === 'step_up') asked = d.authorization_id;
  }
  const res = await client.revoke(run.run_id);
  const after = await client.getRun(run.run_id);
  ok(
    res.declined_step_ups?.length === 1 && res.declined_step_ups[0] === asked,
    'revoking declines the step-up waiting on the customer',
    `asked ${asked}, declined ${JSON.stringify(res.declined_step_ups)}`,
  );
  ok(after.counters.final.waiting === 0, 'and nothing is left waiting on the phone', JSON.stringify(after.counters.final));
}

/* 4b — the settings catalogue offers nothing the engine does not enforce ─ */

console.log('\n— the editable settings —');
{
  const config = await client.config();
  // `/v1/config` strips the prefix; the recorded fixture config keeps it.
  const checks = new Set(config.checks.map((c) => c.replace(/^check_/, '')));
  const inert = Object.entries(SETTING_META).filter(
    ([, m]) => m.check !== 'evaluator.combine' && !checks.has(m.check),
  );
  ok(
    inert.length === 0,
    `all ${Object.keys(SETTING_META).length} settings name a check the engine runs`,
    inert.map(([k, m]) => `${k} → ${m.check}`).join(', '),
  );
  // Layerable since multi-window enrichment. While the shortest window displaced the
  // others, a standing monthly ceiling under an errand's weekly one deleted the monthly one.
  ok(
    SETTING_META.period_limit_chf.standing,
    'the period limit can be standing — every window a policy names is enforced',
  );
  ok(
    TASK_ONLY_FACETS.every((k) => !SETTING_META[k].standing),
    'item identity and attributes belong to the errand, not to the customer',
  );
}

/* 4c — narrowing applies; widening asks ─────────────────────────────── */

console.log('\n— tighten applies, widen asks —');
{
  const run = await client.createRun('SCEN0004');
  const narrow = await client.amend(run.run_id, { per_order_limit_chf: 250 });
  ok(narrow.kind === 'tighten' && narrow.applied, 'a lower cap applies immediately', narrow.kind);

  const kept = (narrow.ir.rules ?? []).filter((r) => r.scope === 'purchase').map((r) => Number(r.value));
  ok(
    kept.includes(400) && kept.includes(250),
    'add-only: what the customer first agreed to stays on the record',
    kept.join(' / '),
  );

  const wide = await client.amend(run.run_id, { per_order_limit_chf: 600 });
  ok(wide.kind === 'widen' && wide.applied === false, 'a higher cap applies nothing yet', wide.kind);
  ok(Boolean(wide.awaiting), 'and waits for a new confirmation', wide.awaiting);

  const dropped = await client.amend(run.run_id, { no_additions: false });
  ok(dropped.kind === 'widen', 'dropping a rule the customer agreed to also asks', dropped.kind);

  let conflict = null;
  try {
    await client.amend(run.run_id, { merchant_type: { merchant_category_in: [] } });
  } catch (e) {
    conflict = e.code;
  }
  ok(conflict === 'preference_conflict', 'an edit that allows nothing is refused', `got ${conflict}`);
}

/* 4c2 — period limits are compared window by window ─────────────────── */

console.log('\n— period windows —');
{
  const period = (value, days) => ({
    field: 'billing_amount_chf', operator: '<=', value, scope: 'period', period_days: days,
  });
  const pol = (rules) => ({ hard_rules: rules, intent_facets: [], uncertainty_policy: 'ask' });

  ok(
    classify(pol([period(300, 7)]), pol([period(200, 7)])).kind === 'tighten',
    'a lower cap on the same window narrows',
  );
  ok(
    classify(pol([period(300, 7)]), pol([period(300, 7), period(1000, 30)])).kind === 'tighten',
    'a monthly ceiling added beside a weekly one narrows',
  );
  // The §3.4 counter-example. Still a widening — but now because a ceiling went, not
  // because period edits are categorically suspect.
  const swapped = classify(pol([period(1000, 30)]), pol([period(500, 7)]));
  ok(swapped.kind === 'widen', 'swapping one window for another widens');
  ok(
    swapped.changes.some((c) => c.why.includes('dropping the 30-day ceiling')),
    'and says which ceiling it dropped',
    swapped.changes.map((c) => c.why).join(' ; '),
  );

  const both = compose(
    { source: 'preferences', policy: preferencesToPolicy({ period_limit_chf: { value: 1000, period_days: 30 } }) },
    { source: 'mandate', policy: pol([period(300, 7)]) },
  );
  ok(
    periodCaps(both.policy.hard_rules).map(([d]) => d).join(',') === '7,30',
    'a standing monthly ceiling composes beside an errand weekly one',
    periodCaps(both.policy.hard_rules).map(([d]) => d).join(','),
  );
}

/* 4c3 — the standing-only rules: exclusions, hours, sensitivity ─────── */

console.log('\n— exclusions, hours and sensitivity —');
{
  const run = await client.createRun('SCEN0001');
  // CU0001 — "avoids gift vouchers". SCEN0001's baskets are groceries, so excluding
  // groceries is the version of that rule this scenario can actually demonstrate.
  await client.setPreferences({ category_exclusion: { item_category_not_in: ['groceries'] } });
  let declined = 0;
  let sample = null;
  let ev;
  while ((ev = await client.nextEvent(run.run_id))) {
    const d = await client.decide(run.run_id, ev.id);
    if (d.reason_codes.includes('item_category_excluded')) {
      declined++;
      sample ??= d;
    }
  }
  ok(declined > 0, `an item exclusion refuses ${declined} of the scenario's orders`);
  // Verified word for word against the engine on 2026-09-21:
  //   PUT /v1/preferences {"category_exclusion": {...}} then POST /v1/decide on AU0002.
  const expected =
    'Declined: CHF 44.50 at Alpine Basket. This order contains groceries, which you do not ' +
    'buy. An order without groceries would meet your preferences.';
  ok(
    sample?.customer_message === expected,
    'and the message says so in the engine’s own wording',
    sample?.customer_message,
  );

  // AU0027-AU0030 land at 02:00-03:00 UTC. A daytime-only rule catches them.
  const hours = createMockClient();
  const run2 = await hours.createRun('SCEN0003');
  await hours.setPreferences({ spending_hours: { hours_from: 7, hours_to: 22 } });
  let night = 0;
  while ((ev = await hours.nextEvent(run2.run_id))) {
    const d = await hours.decide(run2.run_id, ev.id);
    if (d.reason_codes.includes('outside_spending_hours')) night++;
  }
  ok(night > 0, `stated hours refuse ${night} night-time orders`);

  // `make tune-sweep`: the board is flat to 2.0, and four step-ups become approvals at 2.5.
  const looser = createMockClient();
  const run3 = await looser.createRun('SCEN0004');
  const before = { approve: 0, step_up: 0, decline: 0 };
  while ((ev = await looser.nextEvent(run3.run_id))) before[(await looser.decide(run3.run_id, ev.id)).decision]++;

  const relaxed = createMockClient();
  const run4 = await relaxed.createRun('SCEN0004');
  await relaxed.setPreferences({ step_up_threshold: 'less' });
  const after = { approve: 0, step_up: 0, decline: 0 };
  while ((ev = await relaxed.nextEvent(run4.run_id))) after[(await relaxed.decide(run4.run_id, ev.id)).decision]++;
  ok(
    after.step_up < before.step_up,
    `"only ask me when it is serious" turns ${before.step_up - after.step_up} step-up(s) into approvals`,
    `${before.step_up} -> ${after.step_up}`,
  );
  ok(after.decline === before.decline, 'and changes no decline — a threshold never overrides a stated rule');
}

/* 4d — an edit shows its blast radius before it is applied ───────────── */

console.log('\n— the blast radius —');
{
  const run = await client.createRun('SCEN0004');
  const before = client._internal.previewAmend(run.run_id, { per_order_limit_chf: 250 });
  ok(before.flips > 0, `tightening to CHF 250 would change ${before.flips} of ${before.ahead} orders ahead`);

  // Preview must not mutate: the board has to replay identically afterwards.
  const board = { approve: 0, decline: 0, step_up: 0 };
  let ev;
  while ((ev = await client.nextEvent(run.run_id))) board[(await client.decide(run.run_id, ev.id)).decision]++;
  const fixture = ENGINE_OUTPUT.scenarios.find((s) => s.id === 'SCEN0004').events;
  const expectBoard = fixture.reduce((a, e) => ({ ...a, [e.decision]: (a[e.decision] ?? 0) + 1 }), {});
  ok(
    JSON.stringify(board) === JSON.stringify({ approve: expectBoard.approve ?? 0, decline: expectBoard.decline ?? 0, step_up: expectBoard.step_up ?? 0 }),
    'previewing an edit changes nothing',
    JSON.stringify(board),
  );
}

/* 4e — the standing layer: tightest wins, and it names itself ────────── */

console.log('\n— the standing preferences layer —');
{
  const run = await client.createRun('SCEN0004');
  await client.setPreferences({ per_order_limit_chf: 250 });
  let au0038 = null;
  let ev;
  while ((ev = await client.nextEvent(run.run_id))) {
    const d = await client.decide(run.run_id, ev.id);
    if (ev.id === 'AU0038') au0038 = d;
  }
  // Verified word for word against the engine on 2026-09-21:
  //   PUT /v1/preferences {"per_order_limit_chf": 250} then POST /v1/decide
  const expected =
    'Declined: CHF 391.50 (USD 450.00) at HarborByte. CHF 391.50 exceeds the CHF 250.00 ' +
    'per-order limit you set in your preferences. An order of CHF 250.00 or less would be within your limit.';
  ok(au0038?.decision === 'decline', 'a standing CHF 250 preference binds under a CHF 400 mandate');
  ok(au0038?.customer_message === expected, 'and the message says which limit bound', au0038?.customer_message);

  const loose = createMockClient();
  const run2 = await loose.createRun('SCEN0004');
  await loose.setPreferences({ per_order_limit_chf: 5000 });
  let au0037 = null;
  while ((ev = await loose.nextEvent(run2.run_id))) {
    const d = await loose.decide(run2.run_id, ev.id);
    if (ev.id === 'AU0037') au0037 = d;
  }
  ok(au0037?.decision === 'decline', 'a looser preference never widens the mandate', au0037?.decision);

  let refused = null;
  try {
    await loose.setPreferences({ item_attribute: { size: 43 } });
  } catch (e) {
    refused = e.code;
  }
  ok(refused === 'setting_not_standing', 'a per-errand setting is refused as standing', `got ${refused}`);
}

/* 4f — the profile copy has not drifted from the pack ────────────────── */

console.log('\n— the customer profile —');
{
  const readCsv = (name) => {
    const text = readFileSync(new URL(`../viseca-2026/data/${name}`, import.meta.url), 'utf8').trim();
    const [head, ...rows] = text.split('\n');
    const cols = parseRow(head);
    return rows.map((r) => Object.fromEntries(parseRow(r).map((v, i) => [cols[i], v])));
  };
  const index = (rows, key) => Object.fromEntries(rows.map((r) => [r[key], r]));

  const customers = index(readCsv('customers.csv'), 'customer_id');
  const cards = index(readCsv('cards.csv'), 'card_id');
  const accounts = index(readCsv('accounts.csv'), 'account_id');
  const authorities = index(readCsv('scenario_authorities.csv'), 'authority_id');
  const attempts = readCsv('purchase_attempts.csv');

  let drift = [];
  for (const [scenarioId, profile] of Object.entries(PROFILES)) {
    const attempt = attempts.find((a) => a.scenario_id === scenarioId);
    const customer = customers[authorities[attempt.authority_id].customer_id];
    const account = accounts[cards[attempt.card_id].account_id];
    if (customer.customer_id !== profile.customer_id) drift.push(`${scenarioId} customer`);
    if (customer.persona_name !== profile.persona_name) drift.push(`${scenarioId} name`);
    if (customer.budget_style !== profile.budget_style) drift.push(`${scenarioId} budget_style`);
    if (customer.shopping_preferences !== profile.shopping_preferences) drift.push(`${scenarioId} preferences`);
    if (account.account_id !== profile.account.account_id) drift.push(`${scenarioId} account`);
    if (Number(account.per_transaction_limit_chf) !== profile.account.per_transaction_limit_chf)
      drift.push(`${scenarioId} per_transaction_limit`);
  }
  ok(drift.length === 0, `all ${Object.keys(PROFILES).length} profiles match the data pack`, drift.join(', '));

  const proposals = candidatesFor('SCEN0004').candidates;
  ok(
    proposals.every((c) => c.source === 'profile' && c.quote),
    'every profile candidate quotes the field it came from',
  );
  ok(
    proposals.every((c) => SETTING_META[c.setting] && !['money'].includes(SETTING_META[c.setting].control)),
    'a profile never proposes an amount — it states none, so one would be fabricated',
  );
}

/* 4g — the editor's CSS does not restyle another panel ──────────────── */

console.log('\n— css namespaces —');
{
  // The editor's `.step` rule silently turned every row of the AGENT column into a
  // 34px circle, because `.step` was the agent panel's own class and the new block
  // came later in the cascade. Nothing threw; it just looked broken on stage. So a
  // rule in the editor block that keys on a pre-existing class, with no class of its
  // own to scope it, is that bug — and is a test.
  const css = readFileSync(new URL('./src/ui/app.css', import.meta.url), 'utf8');
  const BANNER = '/* ═══ editing the policy ═══';
  const cut = css.indexOf(BANNER);
  ok(cut > 0, 'the editor CSS block is where the guard expects it');

  // Walk the braces rather than splitting on them: the text before each `{` is the
  // selector, and resetting at every brace keeps a nested rule inside an @media block
  // from being read as part of the media prelude.
  const selectorsIn = (text) => {
    const out = [];
    let buf = '';
    for (const ch of text.replace(/\/\*[\s\S]*?\*\//g, '')) {
      if (ch === '{' || ch === '}') {
        if (ch === '{' && buf.includes('.')) out.push(buf.trim());
        buf = '';
      } else buf += ch;
    }
    return out;
  };
  const classesOf = (selector) => [...selector.matchAll(/\.([A-Za-z][\w-]*)/g)].map((m) => m[1]);

  const existing = new Set(selectorsIn(css.slice(0, cut)).flatMap(classesOf));
  const editor = selectorsIn(css.slice(cut));

  // Deliberate extensions: the mandate row became a <button> and the sheet borrows the
  // app's buttons rather than growing a second set.
  const EXTENDS = new Set(['mrow']);

  const clashes = editor.filter((selector) => {
    const classes = classesOf(selector);
    // A selector carrying a class of its own is scoped to the editor — `.sheet__acts
    // .btn--ghost` cannot reach another panel.
    if (classes.some((c) => !existing.has(c))) return false;
    return classes.some((c) => !EXTENDS.has(c));
  });

  ok(
    clashes.length === 0,
    `${editor.length} editor rules, none restyling an existing component`,
    clashes.join(' · '),
  );
}

/* 5 — the agent narrative is derived, and always ends at the leash ───── */

console.log('\n— agent script —');
{
  const all = ENGINE_OUTPUT.scenarios.flatMap((s) => s.events);
  const bad = all.filter((e) => {
    const steps = agentSteps(e);
    return steps.at(-1)?.tool !== 'request_authorization' || steps.length < 4;
  });
  ok(bad.length === 0, 'every order ends in request_authorization', bad.map((e) => e.id).join(', '));
}

console.log(failures === 0 ? '\nAll checks passed.\n' : `\n${failures} check(s) failed.\n`);
process.exit(failures === 0 ? 0 : 1);
