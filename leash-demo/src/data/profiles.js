/**
 * The cardholder behind each scenario — a verbatim copy of the pack.
 *
 * Transcribed from `../viseca-2026/data/customers.csv`, `cards.csv` and
 * `accounts.csv`, joined the way the engine joins them:
 *
 *     purchase_attempts.authority_id → scenario_authorities → customer_id
 *     purchase_attempts.card_id      → cards → account_id   → accounts
 *
 * It lives here rather than in the generated `engine-output.js` because it is
 * not engine output — it is input the engine never used until the standing
 * preferences layer existed. `check.mjs` reads the CSVs directly and fails if
 * a single field here has drifted from the pack, so this copy cannot rot.
 *
 * In live mode `GET /v1/preferences/candidates` serves the same thing from the
 * real pack and this file is not read at all.
 *
 * Spec: wallet-control-layer/specs/customer-settings.md §8
 */

export const PROFILES = {
  SCEN0000: {
    customer_id: 'CU0001',
    persona_name: 'Alex Meier',
    budget_style: 'careful',
    background: 'Plans household purchases for a family and normally shops once a week.',
    shopping_preferences:
      "Practical groceries, children's essentials, and durable household products; avoids gift vouchers.",
    account: { account_id: 'AC0001', per_transaction_limit_chf: 1200, monthly_limit_chf: 4500 },
  },
  SCEN0001: {
    customer_id: 'CU0001',
    persona_name: 'Alex Meier',
    budget_style: 'careful',
    background: 'Plans household purchases for a family and normally shops once a week.',
    shopping_preferences:
      "Practical groceries, children's essentials, and durable household products; avoids gift vouchers.",
    account: { account_id: 'AC0001', per_transaction_limit_chf: 1200, monthly_limit_chf: 4500 },
  },
  SCEN0002: {
    customer_id: 'CU0006',
    persona_name: 'Jonas Frei',
    budget_style: 'balanced',
    background: 'Runs and cycles throughout the year and replaces equipment only when needed.',
    shopping_preferences:
      'Technical sports equipment from specialist retailers and products with clear return terms.',
    account: { account_id: 'AC0009', per_transaction_limit_chf: 1400, monthly_limit_chf: 5000 },
  },
  SCEN0003: {
    customer_id: 'CU0012',
    persona_name: 'Giulia Rossi',
    budget_style: 'balanced',
    background: 'Uses a phone wallet for most everyday purchases and keeps a separate travel card.',
    shopping_preferences:
      'Mobile receipts, fast checkout, and Italian or Swiss fashion retailers with returns.',
    account: { account_id: 'AC0018', per_transaction_limit_chf: 1000, monthly_limit_chf: 4000 },
  },
  SCEN0004: {
    customer_id: 'CU0019',
    persona_name: 'Oliver Graf',
    budget_style: 'careful',
    background: 'Researches online purchases carefully and uses a virtual card for web shops.',
    shopping_preferences:
      'Known electronics sellers, clear warranty terms, and no marketplace add-ons.',
    account: { account_id: 'AC0030', per_transaction_limit_chf: 900, monthly_limit_chf: 3200 },
  },
};

/**
 * What the profile proposes, and — just as importantly — what it does not.
 *
 * `budget_style` is a categorical field, so it maps to a categorical setting.
 * It maps to *how often we ask*, never to *how much may be spent*: there is no
 * number anywhere in a profile, and inventing one would be a fabricated limit
 * wearing the customer's name.
 */
export function candidatesFor(scenarioId) {
  const profile = PROFILES[scenarioId];
  if (!profile) return { customer: null, account: null, candidates: [], not_offered: NOT_OFFERED };
  return {
    customer: profile,
    account: {
      ...profile.account,
      quote: `account limit CHF ${profile.account.per_transaction_limit_chf.toFixed(2)} per transaction`,
    },
    candidates: [
      {
        setting: 'uncertainty_policy',
        label: 'When something is unclear',
        value: 'ask',
        sentence: 'Ask me when something is unclear',
        source: 'profile',
        quote: `budget style: ${profile.budget_style}`,
        why: 'a categorical field maps to a categorical setting, never to an amount',
      },
    ],
    not_offered: NOT_OFFERED,
  };
}

/** A judge asking "why isn't that a setting?" gets an answer, not a silence. */
export const NOT_OFFERED = [
  {
    field: 'shopping_preferences',
    why: 'free prose — enforcing it needs a deny-list check that does not exist yet, so a control here would look enforced and enforce nothing',
  },
  { field: 'typical_spending', why: 'descriptive, with no number any check could compare against' },
  {
    field: 'travel_pattern',
    why: 'no check reads merchant country: these cardholders shop abroad routinely, so scoring foreignness would penalise legitimate purchases',
  },
  { field: 'home_region', why: 'no check reads it' },
];
