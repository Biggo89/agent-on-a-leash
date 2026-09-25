/**
 * The policy algebra the editor needs: compose layers, apply a setting,
 * classify the result.
 *
 * This is the second — and last — place the demo reimplements a rule the
 * engine owns, and for the same reason as the first (`mock.js::applyCap`
 * reimplements `decision-rules.md` §9): the UI has to label a button
 * *before* anything is submitted. "Apply" and "Confirm a new mandate" are
 * different promises, and a screen that learns which one it made only after
 * the round trip is a screen that lied for 200ms.
 *
 * The engine classifies authoritatively when the edit is applied and its
 * answer wins. `check.mjs` asserts this table against the same cases as
 * `wallet-control-layer/tests/unit/test_amend.py`.
 *
 * Spec: wallet-control-layer/specs/customer-settings.md §3 and §6
 */

import { SETTING_META, STANDING_SETTINGS, TASK_ONLY_FACETS } from './client.js';
import { normalise, sourceOf } from './provenance.js';

export const TIGHTEN = 'tighten';
export const WIDEN = 'widen';
export const CONFLICT = 'conflict';
export const NOOP = 'noop';

const RANK = { noop: 0, tighten: 1, widen: 2, conflict: 3 };
const UNCERTAINTY_ORDER = { approve: 0, ask: 1, decline: 2 };

/**
 * What "ask me more / normally / less" means, in the only units the engine has.
 * Measured by `make tune-sweep`, not chosen — see SETTING_META.step_up_threshold.
 * Lower is stricter.
 */
export const STEP_UP_SENSITIVITY = { more: 1.0, normally: 2.0, less: 2.5 };
const DEFAULT_THRESHOLD = 2.0;

const n = (v) => Number(v ?? 0);
const rulesOf = (p) => p?.hard_rules ?? p?.rules ?? [];
const facetsOf = (p) => p?.intent_facets ?? [];
const facetOf = (p, kind) => facetsOf(p).find((f) => f?.kind === kind) ?? null;
const req = (f, key) => f?.require?.[key];

/* ── binding cap — decision-rules.md §9, the tightest of a scope ────────── */

export function bindingCap(rules, scope) {
  const candidates = (rules ?? []).filter(
    (r) => r?.field === 'billing_amount_chf' && r?.scope === scope && ['<=', '<'].includes(r?.operator),
  );
  if (!candidates.length) return null;
  // Ties break toward "<", which excludes the boundary and is the tighter of the two.
  return candidates.reduce((best, r) =>
    n(r.value) < n(best.value) || (n(r.value) === n(best.value) && r.operator === '<') ? r : best,
  );
}

/**
 * Every period constraint the policy states: the binding rule per window.
 *
 * Period rules naming different windows are not comparable by value — 7 days alone permits
 * 1200 in a month, 30 days alone permits 1000 in an afternoon — so all of them are enforced
 * and there is no single binding period rule to ask for. A rule with no usable window keys
 * `null`, and is reported rather than dropped.
 *
 * decision-rules.md §9.
 */
export function periodCaps(rules) {
  const byWindow = new Map();
  const unusable = [];
  for (const r of rules ?? []) {
    if (r?.field !== 'billing_amount_chf' || r?.scope !== 'period') continue;
    if (!['<=', '<'].includes(r?.operator)) continue;
    const days = n(r.period_days);
    if (days > 0) {
      const best = byWindow.get(days);
      if (!best || n(r.value) < n(best.value) || (n(r.value) === n(best.value) && r.operator === '<')) {
        byWindow.set(days, r);
      }
    } else unusable.push(r);
  }
  return [
    ...[...byWindow.keys()].sort((a, b) => a - b).map((days) => [days, byWindow.get(days)]),
    ...unusable.map((r) => [null, r]),
  ];
}

/* ── compose — three layers, tightest wins ─────────────────────────────── */

/**
 * @param {{source:string, policy:object}[]} layers  least to most specific
 * @returns {{policy:object, conflicts:object[], notes:string[]}}
 */
export function compose(...layers) {
  const notes = [];
  const conflicts = [];
  const rules = [];

  // Concatenate. `bindingCap` picks the tightest per purchase scope and `periodCaps` the
  // tightest per window, so the merge needs no logic of its own — and both are monotone,
  // which is what makes "layering never loosens" true by construction.
  for (const { source, policy } of layers) {
    for (const rule of rulesOf(policy)) rules.push(stampIfBare(rule, source));
  }

  const grouped = new Map();
  for (const { source, policy } of layers) {
    for (const f of facetsOf(policy)) {
      if (!f?.kind) continue;
      if (TASK_ONLY_FACETS.includes(f.kind) && source !== 'mandate') {
        notes.push(`setting_not_standing: ${f.kind} describes this errand, not you`);
        continue;
      }
      if (!grouped.has(f.kind)) grouped.set(f.kind, []);
      grouped.get(f.kind).push(stampIfBare(f, source));
    }
  }

  const intent_facets = [];
  for (const [kind, contributors] of grouped) {
    const { facet, conflict } = mergeFacet(kind, contributors);
    if (conflict) conflicts.push(conflict);
    if (facet) intent_facets.push(facet);
  }

  const stated = layers.map((l) => l.policy?.uncertainty_policy).filter((p) => p in UNCERTAINTY_ORDER);
  const uncertainty_policy = stated.length
    ? stated.reduce((a, b) => (UNCERTAINTY_ORDER[b] > UNCERTAINTY_ORDER[a] ? b : a))
    : 'ask';

  // Lower is stricter — it takes less evidence to escalate — so the min is the tightest.
  const thresholds = layers.map((l) => l.policy?.step_up_threshold).filter((t) => Number(t) > 0);
  const policy = { hard_rules: rules, intent_facets, uncertainty_policy };
  if (thresholds.length) policy.step_up_threshold = Math.min(...thresholds.map(Number));

  return { policy, conflicts, notes };
}

const stampIfBare = (carrier, source) =>
  normalise(carrier.provenance).length ? carrier : { ...carrier, provenance: [{ source, quote: '' }] };

function mergeFacet(kind, contributors) {
  if (contributors.length === 1) return { facet: contributors[0], conflict: null };

  const require = {};
  let conflict = null;

  for (const key of ['prior_approvals_min', 'return_window_days_min']) {
    const values = contributors.map((f) => req(f, key)).filter((v) => v != null).map(Number);
    if (values.length) require[key] = Math.max(...values);
  }
  for (const key of ['merchant_category_in', 'item_category_in']) {
    const sets = contributors.map((f) => req(f, key)).filter(Boolean).map((v) => new Set(v.map(String)));
    if (!sets.length) continue;
    let common = [...sets[0]].filter((v) => sets.every((s) => s.has(v)));
    if (!common.length) {
      conflict = {
        code: 'preference_conflict',
        setting: kind,
        message: `no shop in common: ${sets.map((s) => [...s].sort().join(', ')).join(' vs ')}`,
      };
      common = [...new Set(sets.flatMap((s) => [...s]))];
    }
    require[key] = common.sort();
  }
  // union — a deny-list grows: more excluded is tighter, and an exclusion one layer states
  // is not undone by another layer that happens not to mention it.
  for (const key of ['item_category_not_in', 'merchant_category_not_in']) {
    const excluded = [...new Set(contributors.flatMap((f) => req(f, key) ?? []).map(String))].sort();
    if (excluded.length) require[key] = excluded;
  }
  if (kind === 'spending_hours') {
    const sets = contributors.map(hourSet).filter(Boolean);
    if (sets.length) {
      const common = [...sets[0]].filter((h) => sets.every((s2) => s2.has(h)));
      const run = contiguousRun(new Set(common));
      if (!common.length) {
        conflict = { code: 'preference_conflict', setting: kind, message: 'no hour in common' };
      } else if (!run) {
        conflict = { code: 'preference_conflict', setting: kind, message: 'the allowed hours overlap in two separate stretches, which one window cannot express' };
      } else {
        require.hours_from = run[0];
        require.hours_to = run[1];
      }
    }
  }

  const keywords = [...new Set(contributors.flatMap((f) => req(f, 'item_keywords_all') ?? []))].sort();
  if (keywords.length) require.item_keywords_all = keywords;
  for (const key of ['item_description', 'size']) {
    const found = contributors.map((f) => req(f, key)).find((v) => v != null);
    if (found != null) require[key] = found;
  }

  const facet = {
    kind,
    provenance: contributors.flatMap((f) => normalise(f.provenance)),
    confidence: 'high',
  };
  if (Object.keys(require).length || kind !== 'no_additions') facet.require = require;
  return { facet, conflict };
}

/** The hours a window permits, as a set. Null when the bounds are unusable. */
function hourSet(facet) {
  const from = Number(req(facet, 'hours_from'));
  const to = Number(req(facet, 'hours_to'));
  if (!Number.isInteger(from) || !Number.isInteger(to) || from === to) return null;
  if (from < 0 || from > 23 || to < 0 || to > 23) return null;
  const out = new Set();
  for (let h = 0; h < 24; h++) if (from < to ? h >= from && h < to : h >= from || h < to) out.add(h);
  return out;
}

/** `hours` as one half-open (from, to) window, wrapping allowed. Null when it is not one. */
function contiguousRun(hours) {
  if (!hours.size || hours.size === 24) return null;
  for (const start of [...hours].sort((a, b) => a - b)) {
    if (hours.has((start + 23) % 24)) continue;
    let length = 0;
    while (hours.has((start + length) % 24)) length++;
    if (length === hours.size) return [start, (start + length) % 24];
  }
  return null;
}

/* ── apply one setting, semantically ───────────────────────────────────── */

/**
 * Replacement, not addition: a customer who sets 250 means 250, and `classify`
 * has to see that to tell a narrowing from a widening. The add-only list the
 * platform requires is rebuilt by whoever submits the patch.
 */
export function applySettings(ir, settings) {
  let rules = rulesOf(ir).map((r) => ({ ...r }));
  let facets = facetsOf(ir).map((f) => ({ ...f }));

  for (const [key, value] of Object.entries(settings ?? {})) {
    if (key === 'origins') continue;
    const meta = SETTING_META[key];
    if (!meta) continue;

    if (meta.target === 'rule:purchase' || meta.target === 'rule:period') {
      const scope = meta.target === 'rule:purchase' ? 'purchase' : 'period';
      rules = rules.filter((r) => r.scope !== scope);
      if (value != null) {
        const amount = typeof value === 'object' ? value.value : value;
        rules.push({
          field: 'billing_amount_chf',
          operator: '<=',
          value: Number(amount),
          currency: 'CHF',
          scope,
          ...(scope === 'period' ? { period_days: Number(value?.period_days ?? 0) } : {}),
          provenance: [
            {
              source: 'amendment',
              quote: `${meta.label.toLowerCase()} set to CHF ${Number(amount).toFixed(2)} in the app`,
            },
          ],
          confidence: 'high',
        });
      }
    } else if (meta.target === 'uncertainty_policy') {
      ir = { ...ir, uncertainty_policy: String(value) };
    } else if (meta.target === 'step_up_threshold') {
      ir = { ...ir, step_up_threshold: STEP_UP_SENSITIVITY[String(value)] };
    } else if (meta.target.startsWith('facet:')) {
      const kind = meta.target.slice('facet:'.length);
      facets = facets.filter((f) => f.kind !== kind);
      const present = meta.control === 'flag' ? Boolean(value) : value != null;
      if (present) {
        facets.push({
          kind,
          ...(meta.control === 'flag' ? {} : { require: { ...value } }),
          provenance: [{ source: 'amendment', quote: `${meta.label.toLowerCase()} set in the app` }],
          confidence: 'high',
        });
      }
    }
  }
  return { ...ir, rules, hard_rules: rules, intent_facets: facets };
}

/* ── classify — what does this edit cost? ──────────────────────────────── */

const FACET_COMPARISON = {
  merchant_familiarity: ['prior_approvals_min', 'higher'],
  order_terms: ['return_window_days_min', 'higher'],
  merchant_type: ['merchant_category_in', 'subset'],
  item_identity: ['item_category_in', 'subset'],
  item_attribute: ['size', 'exact'],
  no_additions: null,
  // A deny-list is the one requirement that tightens by GROWING.
  category_exclusion: ['item_category_not_in', 'superset'],
  // A window is a set, not a number: "different" is not "narrower".
  spending_hours: [null, 'hours'],
};

export function classify(current, proposed) {
  const changes = [
    ...capChanges(current, proposed),
    ...uncertaintyChanges(current, proposed),
    ...sensitivityChanges(current, proposed),
    ...facetChanges(current, proposed),
  ];
  const kind = changes.reduce((worst, c) => (RANK[c.kind] > RANK[worst] ? c.kind : worst), NOOP);
  return { kind, changes, appliesImmediately: kind === TIGHTEN || kind === NOOP };
}

const capText = (r) => (r == null ? null : r.period_days ? `CHF ${n(r.value).toFixed(2)} / ${n(r.period_days)} days` : `CHF ${n(r.value).toFixed(2)}`);

function capChanges(current, proposed) {
  const out = [];
  const before = bindingCap(rulesOf(current), 'purchase');
  const after = bindingCap(rulesOf(proposed), 'purchase');

  if (!before !== !after) {
    out.push(change('per_order_limit_chf', capText(before), capText(after), before ? WIDEN : TIGHTEN,
      before ? 'removing the per-order limit lets the agent spend without a ceiling' : 'a per-order limit where there was none'));
  } else if (before && after && n(after.value) !== n(before.value)) {
    const tighter = n(after.value) < n(before.value);
    out.push(change('per_order_limit_chf', capText(before), capText(after), tighter ? TIGHTEN : WIDEN,
      tighter ? 'a lower per-order limit' : 'a higher per-order limit lets the agent spend more on one order'));
  }

  out.push(...periodChanges(current, proposed));
  return out;
}

/**
 * Period limits, compared window by window — every one of them is enforced.
 *
 * A window dropped is a ceiling removed whatever happened to the others, and a change of
 * *window* is neither direction on its own: `CHF 500 / 7 days` does not replace
 * `CHF 1000 / 30 days`, it stands beside it.
 */
function periodChanges(current, proposed) {
  const byWindow = (p) => new Map(periodCaps(rulesOf(p)).map(([days, r]) => [days, n(r.value)]));
  const before = byWindow(current);
  const after = byWindow(proposed);
  const text = (m) =>
    m.size
      ? [...m].sort((a, b) => (a[0] ?? 0) - (b[0] ?? 0))
          .map(([d, v]) => (d ? `CHF ${v.toFixed(2)} / ${d} days` : `CHF ${v.toFixed(2)} / no window`))
          .join(', ')
      : 'not set';

  if ([...before.keys()].every((k) => after.get(k) === before.get(k)) && before.size === after.size) return [];

  // A rule the engine cannot enforce is `unknown` at decision time. Uncertainty widens.
  if (before.has(null) || after.has(null)) {
    return [change('period_limit_chf', text(before), text(after), WIDEN,
      'a period limit without a window cannot be enforced, so this needs confirming')];
  }

  const out = [];
  for (const days of [...new Set([...before.keys(), ...after.keys()])].sort((a, b) => a - b)) {
    const was = before.get(days);
    const now = after.get(days);
    if (was === now) continue;
    const label = (v) => `CHF ${v.toFixed(2)} / ${days} days`;
    if (now === undefined) {
      out.push(change('period_limit_chf', label(was), 'not set', WIDEN, `dropping the ${days}-day ceiling you agreed to`));
    } else if (was === undefined) {
      out.push(change('period_limit_chf', 'not set', label(now), TIGHTEN, `a ${days}-day ceiling where there was none`));
    } else {
      const tighter = now < was;
      out.push(change('period_limit_chf', label(was), label(now), tighter ? TIGHTEN : WIDEN,
        `a ${tighter ? 'lower' : 'higher'} ceiling across ${days} days`));
    }
  }
  return out;
}

function uncertaintyChanges(current, proposed) {
  const before = current?.uncertainty_policy ?? 'ask';
  const after = proposed?.uncertainty_policy ?? 'ask';
  if (before === after) return [];
  const tighter = (UNCERTAINTY_ORDER[after] ?? 1) > (UNCERTAINTY_ORDER[before] ?? 1);
  return [change('uncertainty_policy', before, after, tighter ? TIGHTEN : WIDEN,
    tighter ? 'asking or refusing more often when a fact cannot be established'
            : 'resolving unclear cases in the agent’s favour more often')];
}

/** How much evidence it takes to interrupt. Lower is stricter — it escalates sooner. */
function sensitivityChanges(current, proposed) {
  const word = (v) =>
    Object.keys(STEP_UP_SENSITIVITY).find((k) => STEP_UP_SENSITIVITY[k] === Number(v)) ?? String(v);
  const before = Number(current?.step_up_threshold ?? DEFAULT_THRESHOLD);
  const after = Number(proposed?.step_up_threshold ?? DEFAULT_THRESHOLD);
  if (before === after) return [];
  const tighter = after < before;
  return [change('step_up_threshold', word(before), word(after), tighter ? TIGHTEN : WIDEN,
    tighter ? 'stopping to ask on weaker evidence' : 'letting more through without asking you')];
}

function facetChanges(current, proposed) {
  const out = [];
  for (const [kind, comparison] of Object.entries(FACET_COMPARISON)) {
    const before = facetOf(current, kind);
    const after = facetOf(proposed, kind);
    if (!before && !after) continue;
    // An empty allow-list is never a tightening, however it arrived: it reaches the check as
    // UNKNOWN and turns every order into a step-up nobody can connect to the setting. §3.2.
    if (after && comparison?.[1] === 'subset') {
      const values = req(after, comparison[0]);
      if (Array.isArray(values) && !values.length) {
        out.push(change(kind, facetText(before, comparison), 'nothing', CONFLICT, 'nothing would be allowed at all'));
        continue;
      }
    }
    if (!before) { out.push(change(kind, null, facetText(after, comparison), TIGHTEN, 'a new rule')); continue; }
    if (!after) { out.push(change(kind, facetText(before, comparison), null, WIDEN, 'dropping a rule you agreed to')); continue; }
    if (!comparison) continue;
    const [key, direction] = comparison;
    if (direction === 'hours') {
      const c = compareHours(kind, before, after);
      if (c) out.push(c);
      continue;
    }
    const lhs = req(before, key);
    const rhs = req(after, key);
    if (JSON.stringify(lhs) === JSON.stringify(rhs)) continue;
    out.push(compare(kind, lhs, rhs, direction));
  }
  return out;
}

function compare(kind, lhs, rhs, direction) {
  const text = [render(lhs), render(rhs)];
  if (direction === 'higher') {
    const lo = Number(lhs);
    const hi = Number(rhs);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
      return change(kind, ...text, WIDEN, 'the new value could not be compared');
    }
    return change(kind, ...text, hi > lo ? TIGHTEN : WIDEN, hi > lo ? 'a higher bar' : 'a lower bar');
  }
  if (direction === 'superset') {
    const old = new Set((lhs ?? []).map(String));
    const next = new Set((rhs ?? []).map(String));
    const grew = [...old].every((v) => next.has(v)) && next.size > old.size;
    const shrank = [...next].every((v) => old.has(v)) && next.size < old.size;
    if (grew) return change(kind, ...text, TIGHTEN, 'more excluded');
    if (shrank) return change(kind, ...text, WIDEN, 'fewer things excluded');
    return change(kind, ...text, WIDEN, 'exclusions were removed as well as added');
  }

  if (direction === 'subset') {
    const old = new Set((lhs ?? []).map(String));
    const next = new Set((rhs ?? []).map(String));
    if (!next.size) return change(kind, ...text, CONFLICT, 'nothing would be allowed at all');
    const subset = [...next].every((v) => old.has(v));
    const superset = [...old].every((v) => next.has(v));
    if (subset && !superset) return change(kind, ...text, TIGHTEN, 'fewer options allowed');
    if (superset && !subset) return change(kind, ...text, WIDEN, 'more options allowed');
    // Overlapping but neither: some options were traded for others, so the mandate
    // permits something it did not before. Uncertainty widens.
    return change(kind, ...text, WIDEN, 'options were added as well as removed');
  }
  return change(kind, ...text, WIDEN, 'a different requirement, not a narrower one');
}

const change = (setting, before, after, kind, why) => ({
  setting,
  label: SETTING_META[setting]?.label ?? setting,
  before,
  after,
  kind,
  why,
});

/**
 * A window narrows or it does not — and "different" is not "narrower". Shifting one trades
 * hours the customer forbade for hours they allowed, which permits something it did not
 * before, so uncertainty widens.
 */
function compareHours(kind, before, after) {
  const old = hourSet(before);
  const next = hourSet(after);
  const text = [hoursText(before), hoursText(after)];
  if (!old || !next) return change(kind, ...text, WIDEN, 'the hours could not be compared');
  const same = old.size === next.size && [...old].every((h) => next.has(h));
  if (same) return null;
  if ([...next].every((h) => old.has(h))) return change(kind, ...text, TIGHTEN, 'a shorter window');
  if ([...old].every((h) => next.has(h))) return change(kind, ...text, WIDEN, 'a longer window');
  return change(kind, ...text, WIDEN, 'hours were added as well as removed');
}

const hoursText = (facet) => {
  if (!facet) return 'not set';
  const from = req(facet, 'hours_from');
  const to = req(facet, 'hours_to');
  if (from == null || to == null) return 'not set';
  return `${String(from).padStart(2, '0')}:00-${String(to).padStart(2, '0')}:00`;
};

function facetText(facet, comparison) {
  if (!facet) return 'not set';
  if (!comparison) return 'on';
  if (comparison[1] === 'hours') return hoursText(facet);
  return render(req(facet, comparison[0]));
}

function render(value) {
  if (value == null) return 'not set';
  if (Array.isArray(value)) return value.map(String).join(', ').replace(/_/g, ' ') || 'nothing';
  return String(value);
}

/* ── the standing layer, as a policy ───────────────────────────────────── */

export function preferencesToPolicy(prefs) {
  const origins = prefs?.origins ?? {};
  const rules = [];
  const intent_facets = [];
  // A preference the customer set themselves writes no quote: the mandate row already
  // states the rule ("Only electronics shops"), so repeating it underneath as its own
  // provenance says the same thing twice. The source is the origin worth naming. One
  // accepted from the profile keeps its quote — there the origin really is another
  // sentence, the profile field it came from.
  const carry = (key, body) => {
    const origin = origins[key] ?? {};
    return {
      ...body,
      provenance: [{ source: origin.source ?? 'preferences', quote: origin.quote ?? '' }],
      confidence: 'high',
    };
  };

  if (prefs?.period_limit_chf != null) {
    const body = prefs.period_limit_chf;
    rules.push(carry('period_limit_chf', {
      field: 'billing_amount_chf', operator: '<=', value: Number(body.value),
      currency: 'CHF', scope: 'period', period_days: Number(body.period_days),
    }));
  }
  if (prefs?.per_order_limit_chf != null) {
    rules.push(carry('per_order_limit_chf', {
      field: 'billing_amount_chf', operator: '<=', value: Number(prefs.per_order_limit_chf), currency: 'CHF', scope: 'purchase',
    }));
  }
  for (const key of ['merchant_familiarity', 'merchant_type', 'order_terms']) {
    if (prefs?.[key] != null) intent_facets.push(carry(key, { kind: key, require: { ...prefs[key] } }));
  }
  for (const key of ['category_exclusion', 'spending_hours']) {
    if (prefs?.[key] != null) intent_facets.push(carry(key, { kind: key, require: { ...prefs[key] } }));
  }
  if (prefs?.no_additions) intent_facets.push(carry('no_additions', { kind: 'no_additions' }));

  return {
    hard_rules: rules,
    rules,
    intent_facets,
    uncertainty_policy: prefs?.uncertainty_policy ?? null,
    step_up_threshold: STEP_UP_SENSITIVITY[String(prefs?.step_up_threshold ?? '')] ?? null,
  };
}

const DAY_NAMES = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' };
const WEEK = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

/**
 * The label for a facet whose requirement can state several terms at once: a return window
 * and "refundable only", excluded kinds and excluded product words, hours and days. Each term
 * the engine reads is named, so nothing it enforces goes unmentioned on the review screen.
 * Mirrors the engine's checks (specs/check-order-terms.md §"Cancellation",
 * check-category-exclusion.md §"Product words", check-spending-hours.md §"Days").
 * Returns null for any other kind.
 */
export function termsLabel(kind, require) {
  const r = require ?? {};
  const parts = [];
  if (kind === 'order_terms') {
    if (r.return_window_days_min != null) parts.push(`Returnable within ${r.return_window_days_min}+ days`);
    if (r.cancellable === true) parts.push(parts.length ? 'refundable only' : 'Refundable orders only');
    return parts.join(', ') || 'Order terms';
  }
  if (kind === 'category_exclusion') {
    if (r.item_category_not_in?.length) parts.push(`never ${r.item_category_not_in.join(', ').replace(/_/g, ' ')}`);
    if (r.merchant_category_not_in?.length) parts.push(`never from ${r.merchant_category_not_in.join(', ').replace(/_/g, ' ')} shops`);
    if (r.item_keywords_none?.length) parts.push(`nothing named ${r.item_keywords_none.join(', ')}`);
    const text = parts.join('; ');
    return text ? text[0].toUpperCase() + text.slice(1) : 'Nothing excluded';
  }
  if (kind === 'spending_hours') {
    const pad = (h) => String(h).padStart(2, '0');
    if (r.hours_from != null && r.hours_to != null) parts.push(`Only between ${pad(r.hours_from)}:00 and ${pad(r.hours_to)}:00`);
    const days = WEEK.filter((d) => (r.weekdays ?? []).includes(d));
    if (days.length) {
      const run = days.length > 2 && days.join() === WEEK.slice(WEEK.indexOf(days[0]), WEEK.indexOf(days.at(-1)) + 1).join();
      const named = run ? `${DAY_NAMES[days[0]]} to ${DAY_NAMES[days.at(-1)]}` : days.map((d) => DAY_NAMES[d]).join(', ');
      parts.push(`${parts.length ? 'only' : 'Only'} ${named} (Swiss time)`);
    }
    return parts.join(', ') || 'Spending hours';
  }
  return null;
}

/** The sentence a preference shows beside itself. Mirrors `preferences.label_for`. */
export function preferenceSentence(key, prefs) {
  const value = prefs?.[key];
  switch (key) {
    case 'per_order_limit_chf':
      return `Never more than CHF ${Number(value ?? 0).toFixed(2)} on one order`;
    case 'period_limit_chf':
      return `Never more than CHF ${Number(value?.value ?? 0).toFixed(2)} across any ${Number(value?.period_days ?? 0)} days`;
    case 'merchant_familiarity': {
      const min = Number(value?.prior_approvals_min ?? 1);
      return min <= 1 ? 'Only shops I have used before' : `Only shops I have used ${min}+ times`;
    }
    case 'merchant_type':
      return `Only ${(value?.merchant_category_in ?? []).join(', ').replace(/_/g, ' ')} shops`;
    case 'order_terms':
      return `Returnable within ${Number(value?.return_window_days_min ?? 0)}+ days`;
    case 'no_additions':
      return 'Nothing added I did not ask for';
    case 'step_up_threshold':
      return {
        more: 'Ask me at the first sign of trouble',
        normally: 'Ask me when something looks wrong',
        less: 'Only ask me when it is serious',
      }[String(value)] ?? `Ask me: ${value}`;
    case 'spending_hours':
      return `Only between ${String(value?.hours_from ?? 0).padStart(2, '0')}:00 and ${String(value?.hours_to ?? 0).padStart(2, '0')}:00`;
    case 'category_exclusion': {
      const parts = [];
      if (value?.item_category_not_in?.length) parts.push('never ' + value.item_category_not_in.join(', ').replace(/_/g, ' '));
      if (value?.merchant_category_not_in?.length) parts.push('never from ' + value.merchant_category_not_in.join(', ').replace(/_/g, ' ') + ' shops');
      return parts.join('; ') || 'Nothing excluded';
    }
    case 'uncertainty_policy':
      return value === 'ask' ? 'Ask me when unclear' : `When unclear: ${value}`;
    default:
      return SETTING_META[key]?.label ?? key;
  }
}

/** Read the current value of one setting out of a policy or IR — for the editor. */
export function readSetting(policy, key) {
  const meta = SETTING_META[key];
  if (!meta) return null;
  if (meta.target === 'rule:purchase') {
    const cap = bindingCap(rulesOf(policy), 'purchase');
    return cap ? n(cap.value) : null;
  }
  if (meta.target === 'rule:period') {
    // The shortest window is the one a single-window mandate has, and the one the editor
    // opens on. Every window still composes; this only decides what the sheet shows first.
    const [days, rule] = periodCaps(rulesOf(policy))[0] ?? [];
    return rule ? { value: n(rule.value), period_days: days ?? 0 } : null;
  }
  if (meta.target === 'uncertainty_policy') return policy?.uncertainty_policy ?? 'ask';
  if (meta.target === 'step_up_threshold') {
    const current = Number(policy?.step_up_threshold ?? DEFAULT_THRESHOLD);
    return Object.keys(STEP_UP_SENSITIVITY).find((k) => STEP_UP_SENSITIVITY[k] === current) ?? 'normally';
  }
  const kind = meta.target.slice('facet:'.length);
  const found = facetOf(policy, kind);
  if (!found) return meta.control === 'flag' ? false : null;
  return meta.control === 'flag' ? true : { ...(found.require ?? {}) };
}

export { facetOf, rulesOf, facetsOf, sourceOf, STANDING_SETTINGS };
