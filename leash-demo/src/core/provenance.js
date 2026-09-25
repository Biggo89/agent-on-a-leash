/**
 * Where a rule came from.
 *
 * Mirrors `wallet-control-layer/src/leash/domain/provenance.py`. Until the
 * policy had layers, every rule carried one string: the verbatim span of the
 * instruction it was compiled from, which the Leash column highlights inside
 * the cardholder's own sentence. A rule contributed by the standing
 * preferences layer has no span in that sentence, and a facet merged from two
 * layers has two origins — so provenance became a list of `{source, quote}`.
 *
 * A bare string still reads as `instruction`, which is why the five pack
 * mandates and every fixture render exactly as they did before.
 *
 * Spec: wallet-control-layer/specs/customer-settings.md §5
 */

export const SOURCES = ['instruction', 'preferences', 'profile', 'amendment', 'account'];

/** How a row in the mandate card names its origin. */
export const SOURCE_LABEL = {
  instruction: 'your instruction',
  preferences: 'your preferences',
  profile: 'your profile',
  amendment: 'an edit in the app',
  account: 'your account',
};

export function normalise(value) {
  if (value == null || value === '') return [];
  if (typeof value === 'string') return [{ source: 'instruction', quote: value }];
  if (Array.isArray(value)) return value.flatMap(normalise);
  if (typeof value === 'object') {
    const source = SOURCES.includes(value.source) ? value.source : 'instruction';
    return [{ source, quote: String(value.quote ?? '') }];
  }
  return [];
}

/** The layer a rule or facet came from. First entry wins, as in the engine. */
export function sourceOf(carrier) {
  const entries = normalise(carrier?.provenance);
  return entries.length ? entries[0].source : 'instruction';
}

/** Spans to highlight, for one source. The Leash column highlights `instruction`. */
export function quotes(carrier, source = 'instruction') {
  return normalise(carrier?.provenance)
    .filter((e) => e.source === source && e.quote)
    .map((e) => e.quote);
}

/**
 * The line under a mandate row.
 *
 * Quoting is what makes the compile step checkable, so an instruction-sourced
 * rule quotes the customer's own words and says it is quoting them. Every
 * other layer writes its own sentence — "per-order limit set to CHF 250.00 in
 * the app" — which already says where it came from, so wrapping it in
 * "from an edit in the app: …" only repeats the label back. The bare sentence
 * is the origin; the label is the fallback when a layer wrote none.
 */
export function provText(carrier) {
  const entries = normalise(carrier?.provenance);
  if (!entries.length) return '';
  return entries
    .map((e) =>
      e.source === 'instruction'
        ? `from “${e.quote}”`
        : e.quote || `from ${SOURCE_LABEL[e.source]}`,
    )
    .join(' · ');
}
