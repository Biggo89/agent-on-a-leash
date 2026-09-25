/**
 * The one interface the UI is allowed to know about.
 *
 * Every method mirrors an endpoint in
 * `wallet-control-layer/specs/service-contract.md`, with the same shapes.
 * The UI imports this file and nothing below it — swap the adapter and the
 * screens do not change.
 *
 *   MockClient  → replays real engine output, no server, no network
 *   HttpClient  → the actual service on http://127.0.0.1:8000
 *
 * @typedef {'approve'|'decline'|'step_up'} Decision
 * @typedef {'pass'|'concern'|'violation'|'unknown'|'not_applicable'} Verdict
 *
 * @typedef {Object} Evidence
 * @property {string} field
 * @property {string} value
 *
 * @typedef {Object} CheckResult
 * @property {string} check_id
 * @property {Verdict} verdict
 * @property {string|null} reason_code
 * @property {string} detail
 * @property {string} follow_up
 * @property {number|null} weight
 * @property {Evidence[]} evidence
 *
 * @typedef {Object} DecisionRecord
 * @property {string} authorization_id
 * @property {Decision} decision
 * @property {string[]} reason_codes
 * @property {string} customer_message
 * @property {CheckResult[]} checks
 * @property {{concern_score:number, step_up_threshold:number}} score
 * @property {{latency_ms:number}} timing
 *
 * @typedef {Object} LeashClient
 * @property {() => Promise<Object>} health
 * @property {() => Promise<Object>} config
 * @property {() => Promise<Object[]>} listScenarios
 * @property {(instruction:string) => Promise<Object>} compile
 * @property {(scenarioId:string) => Promise<Object>} createRun
 * @property {(runId:string) => Promise<Object>} getRun
 * @property {(runId:string) => Promise<Object|null>} nextEvent
 * @property {(runId:string, authId:string) => Promise<DecisionRecord>} decide
 * @property {(authId:string, decision:'approve'|'decline') => Promise<Object>} resolveStepUp
 * @property {(runId:string, capChf:number) => Promise<Object>} tighten
 * @property {(runId:string) => Promise<Object>} revoke
 */

export const DECISIONS = /** @type {const} */ (['approve', 'decline', 'step_up']);

export const VERDICT_RANK = {
  violation: 0,
  concern: 1,
  unknown: 2,
  pass: 3,
  not_applicable: 4,
};

/** Human labels for the sixteen checks, in evaluation order. */
export const CHECK_META = {
  per_order_limit:       { name: 'Per-order limit',          asks: 'Is this one order within the cap you wrote?' },
  period_limit:          { name: 'Rolling-period limit',     asks: 'Would this push the last N days over your total?' },
  split_order:           { name: 'Split order',              asks: 'Do two orders minutes apart add up past the cap?' },
  merchant_permitted:    { name: 'Merchant familiarity',     asks: 'Has this card — or this person — bought here before?' },
  merchant_type:         { name: 'Merchant type',            asks: 'Is this the kind of shop you named?' },
  merchant_lookalike:    { name: 'Lookalike merchant',       asks: 'Does this name resemble one the card really uses?' },
  order_terms:           { name: 'Order terms',              asks: 'Can it be returned within the window you set?' },
  item_matches_request:  { name: 'Item identity',            asks: 'Is the thing you asked for in this basket at all?' },
  item_attributes:       { name: 'Item attributes',          asks: 'Is the matching item the right variant?' },
  unrequested_addon:     { name: 'Unrequested add-on',       asks: 'Is there something here you never asked for?' },
  goal_fulfilled:        { name: 'Already bought',           asks: 'Was the one thing you asked for already bought?' },
  duplicate_order:       { name: 'Duplicate vs re-quote',    asks: 'Already placed — or a legitimate retry?' },
  category_exclusion:    { name: 'Things you never buy',      asks: 'Is anything here on your do-not-buy list?' },
  spending_hours:        { name: 'Hours you allow',           asks: 'Was it placed inside the hours you set?' },
  session_integrity:     { name: 'Session integrity',        asks: 'Does it look like someone else is driving?' },
  manipulation_detected: { name: 'Merchant-text manipulation', asks: "Is the seller's text instructing the control layer, in any of four languages?" },
};

/**
 * The settings a customer may edit.
 *
 * Mirrors `GET /v1/config`'s `settings` table, which is generated from
 * `wallet-control-layer/src/leash/domain/settings.py`. `check.mjs` asserts
 * this copy against the engine's check list, because the one rule that
 * governs the whole editor is:
 *
 *   **a setting no check reads is not "unsupported", it is inert.**
 *
 * It renders on the customer's screen as an enforced rule and enforces
 * nothing, which is worse than not offering it — they believe they are
 * protected. So every entry names the check that reads it.
 *
 *   standing    may the persistent preferences layer set it, or is it this
 *               errand's business only?
 *   tighter     which direction narrows the mandate
 *   replayable  can the mock adapter re-decide the board from fixture facts,
 *               or does honest feedback need the live engine? See mock.js.
 *
 * Spec: wallet-control-layer/specs/customer-settings.md §7
 */
export const SETTING_META = {
  per_order_limit_chf: {
    label: 'Per-order limit', control: 'money', target: 'rule:purchase',
    check: 'per_order_limit', tighter: 'lower', standing: true, replayable: true,
    help: 'The most the agent may spend on any single order.',
  },
  period_limit_chf: {
    label: 'Limit across a period', control: 'money_window', target: 'rule:period',
    windows: [7, 14, 30, 90],
    check: 'period_limit', tighter: 'lower', standing: true, replayable: false,
    // Layerable since multi-window enrichment: every window a policy names is enforced, so a
    // standing monthly ceiling and an errand's weekly one are two constraints rather than a
    // contest one of them loses. `replayable: false` because the recorded fixtures carry one
    // window's ledger — the live engine is what re-decides a second one.
    help: 'The most the agent may spend across a rolling window.',
  },
  uncertainty_policy: {
    label: 'When something is unclear', control: 'choice', target: 'uncertainty_policy',
    check: 'evaluator.combine', tighter: 'toward_decline', standing: true, replayable: true,
    options: [
      { value: 'ask', label: 'Ask me' },
      { value: 'decline', label: 'Refuse it' },
    ],
    help: 'What happens when a fact cannot be established.',
  },
  merchant_familiarity: {
    label: 'Only shops I have used', control: 'integer', target: 'facet:merchant_familiarity',
    check: 'merchant_permitted', tighter: 'higher', standing: true, replayable: true,
    key: 'prior_approvals_min', min: 0, max: 10,
    help: 'How many past approved purchases a shop needs before the agent may use it.',
  },
  merchant_type: {
    label: 'Kinds of shop allowed', control: 'categories', target: 'facet:merchant_type',
    check: 'merchant_type', tighter: 'subset', standing: true, replayable: true,
    key: 'merchant_category_in',
    help: 'The agent may only buy from these kinds of shop.',
    // The pack's closed vocabulary, from merchants.csv. A value outside it cannot enter a
    // policy even if the customer types it, so offering anything else would be a dead end.
    options: [
      'books', 'cash_withdrawal', 'clothing', 'dining', 'electronics', 'entertainment',
      'food_delivery', 'fuel', 'groceries', 'health', 'home_improvement', 'hotel',
      'household', 'kids_family', 'pet_care', 'photography', 'software', 'sporting_goods',
      'subscriptions', 'sustainable_goods', 'transport', 'travel',
    ],
  },
  order_terms: {
    label: 'Minimum return window', control: 'integer', target: 'facet:order_terms',
    check: 'order_terms', tighter: 'higher', standing: true, replayable: true,
    key: 'return_window_days_min', min: 0, max: 90, unit: 'days',
    help: 'The agent may only buy orders that can be sent back within this many days.',
  },
  no_additions: {
    label: 'Nothing added I did not ask for', control: 'flag', target: 'facet:no_additions',
    check: 'unrequested_addon', tighter: 'present', standing: true, replayable: true,
    help: 'A basket carrying anything beyond the request is refused rather than queried.',
  },
  category_exclusion: {
    label: 'Things I never buy', control: 'exclusions', target: 'facet:category_exclusion',
    check: 'category_exclusion', tighter: 'superset', standing: true, replayable: true,
    // `merchant_type` is an allow-list; the complement of "no gift cards" is the other
    // twenty-one categories — absurd to write, and wrong the moment the vocabulary grows.
    help: 'Kinds of item and kinds of shop the agent may never buy, whatever the errand says.',
    itemOptions: [
      'books', 'clothing', 'cosmetics', 'dining', 'electronics', 'food_delivery', 'fuel',
      'gift_card', 'groceries', 'home_improvement', 'hotel', 'household', 'membership',
      'sporting_goods', 'subscriptions', 'transport',
    ],
    shopOptions: [
      'books', 'cash_withdrawal', 'clothing', 'dining', 'electronics', 'entertainment',
      'food_delivery', 'fuel', 'groceries', 'health', 'home_improvement', 'hotel',
      'household', 'kids_family', 'pet_care', 'photography', 'software', 'sporting_goods',
      'subscriptions', 'sustainable_goods', 'transport', 'travel',
    ],
  },
  spending_hours: {
    label: 'Hours the agent may buy', control: 'hours', target: 'facet:spending_hours',
    check: 'spending_hours', tighter: 'narrower', standing: true, replayable: true,
    help: 'Outside these hours nothing is bought. Read off the purchase’s own clock, in UTC.',
  },
  step_up_threshold: {
    label: 'How often to ask me', control: 'choice', target: 'step_up_threshold',
    check: 'evaluator.combine', tighter: 'lower', standing: true, replayable: true,
    // Measured, not chosen: `make tune-sweep` reports the board flat across [0.5, 2.0] and
    // the nearest cliff at 2.5, where four step-ups become approvals. A free slider would
    // imply a precision the data does not have.
    options: [
      { value: 'more', label: 'At the first sign' },
      { value: 'normally', label: 'Normally' },
      { value: 'less', label: 'Only if serious' },
    ],
    help: 'How much evidence of a problem it takes before the agent stops and asks you.',
  },
  item_identity: {
    label: 'The item asked for', control: 'categories', target: 'facet:item_identity',
    check: 'item_matches_request', tighter: 'subset', standing: false, replayable: false,
    key: 'item_category_in',
    help: 'What this errand is for. Belongs to the errand, not to you.',
  },
  item_attribute: {
    label: 'The variant asked for', control: 'integer', target: 'facet:item_attribute',
    check: 'item_attributes', tighter: 'subset', standing: false, replayable: false,
    key: 'size',
    help: 'Size and other attributes of the requested item.',
  },
};

/** Settings the persistent layer may set — everything else is per-errand. */
export const STANDING_SETTINGS = Object.keys(SETTING_META).filter((k) => SETTING_META[k].standing);

/** Facets that describe the errand's object rather than the customer. */
export const TASK_ONLY_FACETS = ['item_identity', 'item_attribute'];

/** Concern signals and their weights — asserted live against config in the data. */
export const SIGNAL_META = {
  merchant_text_manipulation: { weight: 2, label: "The seller's text instructs the control layer", tier: 'adversarial' },
  merchant_lookalike:         { weight: 2, label: 'The shop name impersonates one the card trusts', tier: 'adversarial' },
  device_novel:               { weight: 2, label: 'A device never seen on this card',               tier: 'adversarial' },
  unrequested_addon:          { weight: 2, label: 'A seller added something to the basket',         tier: 'unagreed spend' },
  duplicate_order:            { weight: 2, label: 'The same committed order placed twice',          tier: 'unagreed spend' },
  velocity_elevated:          { weight: 1, label: 'Several orders within a few minutes',            tier: 'ambient' },
  unusual_hour:               { weight: 1, label: 'Placed during the night',                        tier: 'ambient' },
};
