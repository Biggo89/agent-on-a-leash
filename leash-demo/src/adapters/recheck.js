/**
 * Re-running the editable checks over a fixture, after the customer edits a setting.
 *
 * The demo replays real engine output. The moment a setting changes, the
 * recorded verdict is answering a question the customer no longer asked — so
 * the affected checks have to be recomputed here, and they have to be
 * recomputed *the way the engine does it*, down to the wording, or the demo
 * starts telling a story the engine does not support.
 *
 * Every rule below is transcribed from `wallet-control-layer/src/leash/domain/
 * checks/`, and `check.mjs` asserts each one against the fixtures the engine
 * itself produced. The exact strings matter: `composeMessage` in `mock.js`
 * reassembles the customer message out of these details, and the demo's claim
 * is that tightening SCEN0004 to CHF 250 reproduces the engine's sentence word
 * for word.
 *
 * **Where this is narrower than the engine, and why it is safe.**
 * `order_terms` compares against a return window the engine extracts from the
 * seller's item text. A fixture carries that extracted number only when the
 * scenario already ran the check, so when a customer adds a return-window rule
 * to a scenario that had none, the window is genuinely unknown *here* though
 * the engine would know it. The check then returns `unknown`, which routes
 * through `uncertainty_policy` and asks the customer — the engine's own
 * behaviour for an unestablished fact, and the safe direction. Live mode has
 * no such gap: the real engine decides.
 */

import { SETTING_META } from '../core/client.js';
import { bindingCap, facetOf } from '../core/policy.js';

const n = (v) => Number(v ?? 0);
const f2 = (v) => n(v).toFixed(2);
const req = (f, key) => f?.require?.[key];
const evidenceOf = (check, field) => check?.evidence?.find((e) => e.field === field)?.value;

const readable = (c) => String(c).replace(/_/g, ' ');
const readableList = (cats) => {
  const names = cats.map(readable);
  if (names.length === 1) return `a ${names[0]} shop`;
  return `a ${[names.slice(0, -1).join(', '), names[names.length - 1]].join(' or ')} shop`;
};
const cap1 = (s) => (s ? s[0].toUpperCase() + s.slice(1) : '');

const na = (check_id) => ({
  check_id,
  verdict: 'not_applicable',
  reason_code: null,
  detail: '',
  follow_up: '',
  weight: null,
  evidence: [],
});

/** The checks this module can recompute — everything else keeps its fixture verdict. */
export const RECHECKED = Object.values(SETTING_META)
  .filter((m) => m.replayable && m.check !== 'evaluator.combine')
  .map((m) => m.check);

/**
 * Recompute every editable check for one event under a policy.
 *
 * @param {object} ev       the fixture event, with the engine's original checks
 * @param {object[]} checks the current check list (mutated copy returned)
 * @param {object} policy   the composed effective policy
 */
export function recheck(ev, checks, policy) {
  const out = checks.map((c) => ({ ...c }));
  const replace = (check_id, next) => {
    const i = out.findIndex((c) => c.check_id === check_id);
    if (i >= 0) out[i] = next;
    else out.push(next);
  };
  const original = (check_id) => checks.find((c) => c.check_id === check_id);
  // A check may emit several results; replace them as a group so a second call does not
  // leave the first call's rows behind.
  const replaceAll = (check_id, next) => {
    for (let i = out.length - 1; i >= 0; i--) if (out[i].check_id === check_id) out.splice(i, 1);
    out.push(...next);
  };

  replace('per_order_limit', perOrderLimit(ev, policy));
  replace('split_order', splitOrder(ev, policy));
  replace('merchant_permitted', merchantPermitted(ev, policy));
  replace('merchant_type', merchantType(ev, policy));
  replace('order_terms', orderTerms(ev, policy, original('order_terms')));
  replace('unrequested_addon', unrequestedAddon(policy, original('unrequested_addon')));
  replaceAll('category_exclusion', categoryExclusion(ev, policy));
  replace('spending_hours', spendingHours(ev, policy));
  return out;
}

/* ── per-order limit — checks/limits.py ────────────────────────────────── */

function perOrderLimit(ev, policy) {
  const rule = bindingCap(policy.hard_rules, 'purchase');
  if (!rule) return na('per_order_limit');

  const amount = n(ev.billing_amount_chf);
  const limit = n(rule.value);
  const over = rule.operator === '<' ? amount >= limit : amount > limit;
  // An instruction-sourced limit says nothing extra — "your CHF 200 limit" already refers to
  // words the customer wrote. Any other layer must name itself or they read their own
  // instruction, see a different number, and conclude the engine is wrong.
  const whose = attribution(rule);
  const mine = whose ? 'the ' : 'your ';
  const evidence = [
    { field: 'billing_amount_chf', value: f2(amount) },
    { field: 'per_order_limit_chf', value: f2(limit) },
    { field: 'per_order_limit_source', value: sourceOf(rule) },
  ];

  return over
    ? {
        check_id: 'per_order_limit',
        verdict: 'violation',
        reason_code: 'per_order_limit_exceeded',
        detail: `CHF ${f2(amount)} exceeds the CHF ${f2(limit)} per-order limit${whose}`,
        follow_up: `An order of CHF ${f2(limit)} or less would be within your limit.`,
        weight: null,
        evidence,
      }
    : {
        check_id: 'per_order_limit',
        verdict: 'pass',
        reason_code: 'within_per_order_limit',
        detail: `within ${mine}CHF ${f2(limit)} per-order limit${whose}`,
        follow_up: '',
        weight: null,
        evidence,
      };
}

const ATTRIBUTION = {
  instruction: '',
  preferences: ' you set in your preferences',
  profile: ' from your profile',
  amendment: ' you set in the app',
  account: ' on your account',
};
const sourceOf = (rule) => {
  const p = rule?.provenance;
  if (typeof p === 'string' || p == null) return 'instruction';
  const first = Array.isArray(p) ? p[0] : p;
  return first?.source ?? 'instruction';
};
const attribution = (rule) => ATTRIBUTION[sourceOf(rule)] ?? '';

/* ── merchant familiarity — checks/merchant.py ─────────────────────────── */

function merchantPermitted(ev, policy) {
  const required = facetOf(policy, 'merchant_familiarity');
  if (!required) return na('merchant_permitted');

  const minimum = Number(req(required, 'prior_approvals_min') ?? 1);
  const seen = n(ev.enrichment?.merchant_prior_approvals);
  const evidence = [
    { field: 'merchant_id', value: ev.merchant_id },
    { field: 'merchant_prior_approvals', value: String(seen) },
    { field: 'prior_approvals_required', value: String(minimum) },
  ];

  // Boundary: "used before" is satisfied by meeting the minimum, not exceeding it.
  if (seen >= minimum) {
    return {
      check_id: 'merchant_permitted',
      verdict: 'pass',
      reason_code: 'merchant_familiar',
      detail: `${ev.merchant_name} is a shop you have used before (${seen} previous purchases)`,
      follow_up: '',
      weight: null,
      evidence,
    };
  }

  // Card or person. The customer wrote "shops I have used before" about themselves, and most
  // cardholders hold more than one card. A shop this card has not visited but the person has
  // is `unknown` — never a pass, because reading the person's history as permission would
  // widen the rule they stated. Mirrors checks/merchant.py; spec §"Card or person".
  const acrossCards = n(ev.enrichment?.merchant_prior_approvals_customer);
  if (acrossCards >= minimum) {
    return {
      check_id: 'merchant_permitted',
      verdict: 'unknown',
      reason_code: 'merchant_familiarity_ambiguous',
      detail: `you have bought from ${ev.merchant_name} before, but on a different card and not on this one`,
      follow_up: 'Your instruction names shops you have used, and this card has not been there.',
      weight: null,
      evidence: [
        ...evidence,
        { field: 'merchant_name', value: ev.merchant_name },
        { field: 'merchant_prior_approvals_customer', value: String(acrossCards) },
      ],
    };
  }

  return {
    check_id: 'merchant_permitted',
    verdict: 'violation',
    reason_code: 'merchant_not_permitted',
    detail: `you have not bought from ${ev.merchant_name} before`,
    follow_up: '',
    weight: null,
    evidence: [...evidence, { field: 'merchant_name', value: ev.merchant_name }],
  };
}

/* ── split order — checks/limits.py ────────────────────────────────────── */

/**
 * Two orders at one shop, minutes apart, together over the per-order cap.
 *
 * Mirrors `check_split_order`; spec: `specs/check-split-order.md`. It matters here and not
 * only in the engine because *lowering* a cap is exactly what makes a pair cross it — the
 * blast-radius preview would under-report a tighten without this.
 *
 * Stands down for a duplicate, which already asks with a better message.
 */
function splitOrder(ev, policy) {
  const rule = bindingCap(policy.hard_rules, 'purchase');
  if (!rule) return na('split_order');

  // No reason code on a pass: an ordinary single order must not lengthen its own approval.
  const quiet = (evidence = []) => ({
    check_id: 'split_order',
    verdict: 'pass',
    reason_code: null,
    detail: '',
    follow_up: '',
    weight: null,
    evidence,
  });
  const recent = ev.enrichment?.same_merchant_recent ?? [];
  if (recent.length === 0) return quiet();
  if (ev.enrichment?.is_duplicate_of) return na('split_order');

  const limit = n(rule.value);
  const total = recent.reduce((sum, e) => sum + n(e.chf), n(ev.billing_amount_chf));
  const orders = recent.length + 1;
  const whose = attribution(rule);
  const evidence = [
    { field: 'split_order_ids', value: recent.map((e) => e.id).join(', ') },
    { field: 'split_order_count', value: String(orders) },
    { field: 'split_order_total_chf', value: f2(total) },
    { field: 'per_order_limit_chf', value: f2(limit) },
    { field: 'per_order_limit_source', value: sourceOf(rule) },
  ];

  // Boundary: equality passes, matching the per-order check.
  const over = rule.operator === '<' ? total >= limit : total > limit;
  if (!over) return quiet(evidence);

  return {
    check_id: 'split_order',
    verdict: 'concern',
    reason_code: 'split_order_suspected',
    detail:
      `${orders} orders at ${ev.merchant_name} within a few minutes come to ` +
      `CHF ${f2(total)}, against the CHF ${f2(limit)} per-order limit${whose}`,
    follow_up: 'Each order is within your limit on its own.',
    weight: 2.0,
    evidence,
  };
}

/* ── merchant type — checks/merchant.py ────────────────────────────────── */

function merchantType(ev, policy) {
  const required = facetOf(policy, 'merchant_type');
  if (!required) return na('merchant_type');

  const allowed = (req(required, 'merchant_category_in') ?? []).map(String);
  const evidence = [
    { field: 'merchant_category', value: ev.merchant_category ?? '' },
    { field: 'merchant_category_allowed', value: allowed.join(', ') || '(unspecified)' },
  ];

  // An unresolved requirement must never silently approve.
  if (!allowed.length) {
    return {
      check_id: 'merchant_type',
      verdict: 'unknown',
      reason_code: 'merchant_type_unknown',
      detail: 'the kind of shop your instruction allows could not be established',
      follow_up: '',
      weight: null,
      evidence,
    };
  }
  if (allowed.includes(ev.merchant_category)) {
    return {
      check_id: 'merchant_type',
      verdict: 'pass',
      reason_code: 'merchant_meets_requirement',
      detail: `${ev.merchant_name} is the kind of shop you asked for (${readable(ev.merchant_category)})`,
      follow_up: '',
      weight: null,
      evidence,
    };
  }
  return {
    check_id: 'merchant_type',
    verdict: 'violation',
    reason_code: 'merchant_type_not_permitted',
    detail: `${ev.merchant_name} is a ${readable(ev.merchant_category)} shop, not ${readableList(allowed)}`,
    follow_up: `${cap1(readableList(allowed))} would meet your instruction.`,
    weight: null,
    evidence,
  };
}

/* ── order terms — checks/order.py ─────────────────────────────────────── */

function orderTerms(ev, policy, fixture) {
  const required = facetOf(policy, 'order_terms');
  if (!required) return na('order_terms');

  const minDays = req(required, 'return_window_days_min');
  const returnable = ev.order_returnable;
  const base = [{ field: 'order_returnable', value: String(returnable) }];

  if (minDays == null) {
    return {
      check_id: 'order_terms',
      verdict: 'unknown',
      reason_code: 'return_terms_unknown',
      detail: 'the return window your instruction requires could not be established',
      follow_up: '',
      weight: null,
      evidence: base,
    };
  }
  const min = Number(minDays);
  base.push({ field: 'return_window_days_required', value: String(min) });

  // The trusted field is authoritative for whether returns exist at all.
  if (returnable === 'false' || returnable === 'not_applicable') {
    return {
      check_id: 'order_terms',
      verdict: 'violation',
      reason_code: 'order_not_returnable',
      detail:
        returnable === 'false'
          ? `this order cannot be returned, and you asked for at least ${min} days`
          : `returns do not apply to this kind of order, and you asked for at least ${min} days`,
      follow_up: `A seller offering ${min} days or more would meet your terms.`,
      weight: null,
      evidence: base,
    };
  }
  // "unknown" means the term was not stated. That is uncertainty, never a negative.
  if (returnable !== 'true') {
    return {
      check_id: 'order_terms',
      verdict: 'unknown',
      reason_code: 'return_terms_unknown',
      detail: 'the seller did not state whether this order can be returned',
      follow_up: '',
      weight: null,
      evidence: base,
    };
  }

  // The engine extracts this from the seller's item text; a fixture carries it only when the
  // scenario already ran the check. See the module docstring — `unknown` is the honest answer
  // and the safe direction.
  const stated = evidenceOf(fixture, 'return_window_days');
  if (stated == null) {
    return {
      check_id: 'order_terms',
      verdict: 'unknown',
      reason_code: 'return_terms_unknown',
      detail: 'the seller did not state how long this order can be returned within',
      follow_up: '',
      weight: null,
      evidence: base,
    };
  }
  const window = Number(stated);
  const evidence = [...base, { field: 'return_window_days', value: String(window) }];

  return window < min
    ? {
        check_id: 'order_terms',
        verdict: 'violation',
        reason_code: 'return_window_too_short',
        detail: `this order can only be returned within ${window} days, and you asked for at least ${min}`,
        follow_up: `A seller offering ${min} days or more would meet your terms.`,
        weight: null,
        evidence,
      }
    : {
        // Boundary: "14 days or more" is >=, so exactly the minimum satisfies it.
        check_id: 'order_terms',
        verdict: 'pass',
        reason_code: 'order_terms_acceptable',
        detail: `this order can be returned within ${window} days, meeting your ${min}-day minimum`,
        follow_up: '',
        weight: null,
        evidence,
      };
}

/* ── category exclusion — checks/exclusion.py ──────────────────────────── */

/** Plain list. The merchant-type one above wraps in "a … shop", which item kinds are not. */
const plainList = (cats) => {
  const names = cats.map(readable);
  return names.length === 1 ? names[0] : [names.slice(0, -1).join(', '), names.at(-1)].join(' or ');
};

const unknownResult = (check_id, detail, evidence = []) => ({
  check_id,
  verdict: 'unknown',
  reason_code: 'insufficient_evidence',
  detail,
  follow_up: '',
  weight: null,
  evidence,
});

/**
 * A deny-list over shop kinds and item kinds. One result per dimension the facet states,
 * merchant first — a cart both from an excluded shop and carrying an excluded item is two
 * findings, and reporting them as one would hide half of it.
 */
function categoryExclusion(ev, policy) {
  const required = facetOf(policy, 'category_exclusion');
  if (!required) return [na('category_exclusion')];

  const shops = (req(required, 'merchant_category_not_in') ?? []).map(String);
  const items = (req(required, 'item_category_not_in') ?? []).map(String);
  if (!shops.length && !items.length) {
    return [
      unknownResult('category_exclusion', 'the kinds of purchase you rule out could not be established'),
    ];
  }

  const out = [];
  if (shops.length) {
    const evidence = [
      { field: 'merchant_category', value: ev.merchant_category ?? '' },
      { field: 'merchant_category_excluded_list', value: shops.join(', ') },
    ];
    if (!ev.merchant_category) {
      out.push(unknownResult('category_exclusion', 'the kind of shop this is could not be established', evidence));
    } else if (shops.includes(ev.merchant_category)) {
      out.push({
        check_id: 'category_exclusion', verdict: 'violation', reason_code: 'merchant_category_excluded',
        detail: `this is a ${readable(ev.merchant_category)} shop, which you do not buy from`,
        follow_up: `A shop that is not ${plainList(shops)} would meet your preferences.`,
        weight: null, evidence,
      });
    } else {
      out.push({
        check_id: 'category_exclusion', verdict: 'pass', reason_code: 'categories_permitted',
        detail: `this is not ${plainList(shops)}, which you do not buy from`,
        follow_up: '', weight: null, evidence,
      });
    }
  }
  if (items.length) {
    const seen = (ev.items ?? []).map((i) => String(i.category ?? '')).filter(Boolean);
    const evidence = [
      { field: 'item_categories', value: [...new Set(seen)].sort().join(', ') },
      { field: 'item_category_excluded_list', value: items.join(', ') },
    ];
    if (!seen.length) {
      out.push(unknownResult('category_exclusion', 'what kind of thing this order contains could not be established', evidence));
    } else {
      // Boundary: one excluded line among many is a violation.
      const offending = [...new Set(seen.filter((c) => items.includes(c)))].sort();
      out.push(
        offending.length
          ? {
              check_id: 'category_exclusion', verdict: 'violation', reason_code: 'item_category_excluded',
              detail: `this order contains ${plainList(offending)}, which you do not buy`,
              follow_up: `An order without ${plainList(offending)} would meet your preferences.`,
              weight: null,
              evidence: [...evidence, { field: 'item_categories_offending', value: offending.join(', ') }],
            }
          : {
              check_id: 'category_exclusion', verdict: 'pass', reason_code: 'categories_permitted',
              detail: `nothing here is ${plainList(items)}, which you do not buy`,
              follow_up: '', weight: null, evidence,
            },
      );
    }
  }
  return out;
}

/* ── spending hours — checks/exclusion.py ──────────────────────────────── */

/**
 * A stated rule about *when* the agent may spend. Not the `unusual_hour` concern, which is
 * our inference about risk. Read off the SIMULATED clock, so a replay decides the same way
 * at midnight as at noon.
 */
function spendingHours(ev, policy) {
  const required = facetOf(policy, 'spending_hours');
  if (!required) return na('spending_hours');

  const hourOf = (v) =>
    Number.isInteger(Number(v)) && Number(v) >= 0 && Number(v) <= 23 ? Number(v) : null;
  const start = hourOf(req(required, 'hours_from'));
  const end = hourOf(req(required, 'hours_to'));
  if (start === null || end === null || start === end) {
    return unknownResult('spending_hours', 'the hours you allow could not be established', [
      { field: 'hours_from', value: String(req(required, 'hours_from')) },
      { field: 'hours_to', value: String(req(required, 'hours_to')) },
    ]);
  }

  const hh = (h) => String(h).padStart(2, '0');
  const hour = new Date(ev.timestamp).getUTCHours();
  const inside = start < end ? hour >= start && hour < end : hour >= start || hour < end;
  const evidence = [
    { field: 'timestamp_hour_utc', value: hh(hour) },
    { field: 'spending_hours_utc', value: `${hh(start)}:00-${hh(end)}:00` },
  ];
  const window = `${hh(start)}:00 and ${hh(end)}:00`;
  return inside
    ? {
        check_id: 'spending_hours', verdict: 'pass', reason_code: 'within_spending_hours',
        detail: `it was placed at ${hh(hour)}:00, inside the hours you allow`,
        follow_up: '', weight: null, evidence,
      }
    : {
        check_id: 'spending_hours', verdict: 'violation', reason_code: 'outside_spending_hours',
        detail: `it was placed at ${hh(hour)}:00, and you only buy between ${window}`,
        follow_up: `An order between ${window} would meet your preferences.`,
        weight: null, evidence,
      };
}

/* ── unrequested add-on — checks/item.py ───────────────────────────────── */

/**
 * One meaning, two strengths. The cart either carries something unrequested or
 * it does not — a fact the fixture already settled — and the **verdict** is
 * what the `no_additions` rule changes: stated, it is a violation; merely
 * implied by naming the item, a concern at weight 2.0.
 */
function unrequestedAddon(policy, fixture) {
  if (!fixture || fixture.reason_code !== 'unrequested_addon') return fixture ?? na('unrequested_addon');
  const stated = facetOf(policy, 'no_additions') != null;
  return {
    ...fixture,
    verdict: stated ? 'violation' : 'concern',
    weight: stated ? null : 2,
  };
}
