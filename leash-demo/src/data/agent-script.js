/**
 * Turns one authorization event into the sequence of agent actions that led to it.
 *
 * HONESTY NOTE — this is the only synthesised part of the demo. The data pack
 * records *what the agent tried to pay for*, not the browsing that preceded it,
 * so the tool calls below are reconstructed from the event's own facts:
 * merchant, category, items, prices, prior-approval counts, and the enrichment
 * flags the engine computed. Every decision, check, verdict, evidence line,
 * concern score, latency and customer message is untouched engine output.
 *
 * The UI labels this distinction on screen.
 */

const CATEGORY_QUERY = {
  groceries: 'household groceries, delivery',
  household: 'household supplies',
  sporting_goods: 'road-running shoes, size 43',
  clothing: 'clothing',
  electronics: '27-inch monitor',
};

const money = (v, c = 'CHF') => `${c} ${Number(v).toFixed(2)}`;

/** Upstream ids carry an environment suffix (AU0035-PLAYGROUND); show the id. */
const shortId = (id) => String(id ?? '').split('-')[0];

/**
 * @returns {{key:string, tool:string, arg:string, note?:string, flag?:string}[]}
 */
export function agentSteps(event, scenario) {
  const steps = [];
  const cat = event.merchant_category;
  const enr = event.enrichment ?? {};
  const items = event.items ?? [];
  const query = CATEGORY_QUERY[cat] ?? event.purchase_description ?? 'items';

  // 1 — intent, restated from the mandate the agent is working under
  if (enr.is_requote_of) {
    steps.push({
      key: 'plan',
      tool: 'plan',
      arg: `re-quote after ${shortId(enr.is_requote_of)} was declined`,
      note: 'Same order, new price from the seller.',
    });
  } else if (enr.is_duplicate_of) {
    steps.push({
      key: 'plan',
      tool: 'plan',
      arg: `re-submit order ${shortId(enr.is_duplicate_of)}`,
      note: 'No response recorded for the previous attempt.',
    });
  } else {
    steps.push({ key: 'plan', tool: 'plan', arg: `buy ${event.purchase_description ?? query}` });
  }

  // 2 — search
  steps.push({
    key: 'search',
    tool: 'search_merchants',
    arg: `"${query}"`,
    note: `category ${cat}`,
  });

  // 3 — merchant selection, carrying the history the engine will read
  const priors = enr.merchant_prior_approvals ?? 0;
  steps.push({
    key: 'select',
    tool: 'select_merchant',
    arg: event.merchant_name,
    note:
      priors > 0
        ? `${priors} previous approved order${priors === 1 ? '' : 's'} on this card`
        : 'no previous orders on this card',
    flag: enr.merchant_lookalike_of ? 'lookalike' : priors === 0 ? 'new-merchant' : null,
  });

  // 4 — reading the product page: where merchant-controlled text enters
  const manip = enr.manipulations ?? [];
  if (manip.length) {
    steps.push({
      key: 'read',
      tool: 'read_product_page',
      arg: items[0]?.name ?? event.purchase_description,
      note: `${manip.length} passage${manip.length === 1 ? '' : 's'} addressed to automated systems`,
      flag: 'injection',
    });
  } else if (items[0]?.details) {
    steps.push({
      key: 'read',
      tool: 'read_product_page',
      arg: items[0].name,
      note: items[0].details.length > 90 ? `${items[0].details.slice(0, 90)}…` : items[0].details,
    });
  }

  // 5 — basket
  for (const it of items) {
    steps.push({
      key: `cart:${it.name}`,
      tool: 'add_to_cart',
      arg: it.name,
      note: `${it.quantity} × ${money(it.unit_price, it.currency)}`,
    });
  }

  // 6 — session context, only when it is actually anomalous
  if ((enr.device_prior_approvals ?? 1) === 0) {
    steps.push({
      key: 'session',
      tool: 'session',
      arg: event.device_id,
      note: 'device not seen on this card before',
      flag: 'device',
    });
  }

  // 7 — the moment it hits the leash
  steps.push({
    key: 'authorize',
    tool: 'request_authorization',
    arg: money(event.amount, event.currency),
    note:
      event.currency !== 'CHF'
        ? `settles at ${money(event.billing_amount_chf)}`
        : null,
    flag: 'authorize',
  });

  return steps;
}

/** One line summarising what the agent did after a decision came back. */
export function agentReaction(decision, event, nextEvent) {
  if (decision === 'approve') return { tone: 'ok', text: 'Order placed.' };
  if (decision === 'step_up') return { tone: 'ask', text: 'Waiting for the cardholder.' };
  if (nextEvent?.enrichment?.is_requote_of === event.id)
    return { tone: 'no', text: 'Order not placed. Asking the seller to re-quote.' };
  return { tone: 'no', text: 'Order not placed.' };
}
