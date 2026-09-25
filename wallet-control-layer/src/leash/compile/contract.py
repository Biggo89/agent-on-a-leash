"""The compile-time guard: what a model is allowed to write into a Policy IR.

A model proposes; this module decides what is accepted. It is pure — no network, no SDK, no
file I/O — so the whole trust argument for the compiler is testable with no key and no data
pack. Spec: specs/llm-compiler.md.

Two things it is NOT:

  * It is not a safety filter over merchant text. Nothing merchant-controlled reaches here;
    the compiler's only input is the cardholder's own instruction (specs/llm-compiler.md,
    "The trust argument" §1).
  * It is not a decision. Nothing in this file runs at decision time.

Every rail either drops something or repairs it toward the deterministic baseline. No rail can
add an enforced restriction the model did not propose, and none can widen one it did.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..domain.checks.item import name_tokens
from ..domain.compose import merge_facets
from ..domain.localtime import DAY_NAMES, WEEKDAYS

# The vocabularies, the facet kinds and the per-kind `require` allowlist all live in
# `domain/settings.py` — one table, read here by the compile guard, by `domain/compose.py`
# when two layers merge, and by `GET /v1/config` so the UI cannot offer a control no check
# reads. They are re-exported so this module's public surface is unchanged.
from ..domain.settings import (  # noqa: E402  # noqa: E402
    CONFIDENCE_LEVELS,
    FACET_KINDS,
    FACET_REQUIRE_KEYS,
    ITEM_CATEGORIES,
    MERCHANT_CATEGORIES,
    RULE_FIELDS,
    RULE_OPERATORS,
    RULE_SCOPES,
    UNCERTAINTY_POLICIES,
    category_vocabulary,
    item_categories,
)
from .currency import mentions, question_for, unconvertible

__all__ = [
    "CONFIDENCE_LEVELS",
    "FACET_KINDS",
    "FACET_REQUIRE_KEYS",
    "ITEM_CATEGORIES",
    "MERCHANT_CATEGORIES",
    "RULE_FIELDS",
    "RULE_OPERATORS",
    "RULE_SCOPES",
    "UNCERTAINTY_POLICIES",
    "Guard",
    "accept",
    "apply_safety_floor",
    "instruction_hash",
    "quotes_instruction",
]

_WHITESPACE = re.compile(r"\s+")
# Rail 11: kinds whose second facet only adds restrictions, so it is merged, not dropped.
_MERGED_KINDS = frozenset({"category_exclusion", "order_terms", "spending_hours"})
# Rail 5c: words that join a phrase and name nothing. Kept out of an excluded phrase, because
# "and" alone is in "Drinks and snacks" and "Bakery and dairy order".
_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "all",
        "for",
        "in",
        "no",
        "not",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


def _category_words() -> frozenset[str]:
    """Rail 5b, condition 1: the words the item-category vocabulary in force is made of. A
    keyword that is one of these names a *kind* of product, which `item_category_in` says."""
    return frozenset(word for category in item_categories() for word in name_tokens(category))


def instruction_hash(instruction: str) -> str:
    """SHA-256 of the instruction as the API will receive it (specs/llm-compiler.md)."""
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()


def _collapsed(text: str) -> str:
    """Whitespace-collapsed, case-folded text, for substring comparison only.

    Provenance must be the customer's own words, but a model that returns "CHF 200" for a
    line-wrapped "CHF\n200" is quoting faithfully. Collapsing whitespace tolerates that and
    nothing else: no stemming, no synonyms, no fuzzy match.
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


def quotes_instruction(provenance: Any, instruction: str) -> bool:
    """Is `provenance` a verbatim span of the instruction? The anti-fabrication rail."""
    if not isinstance(provenance, str) or not provenance.strip():
        return False
    return _collapsed(provenance) in _collapsed(instruction)


def _number(value: Any) -> float | None:
    """A JSON number, or a string a model wrote a number into. Never a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


class Guard:
    """Accumulates what was accepted and, for every rejection, why.

    `notes` is the compile-step audit trail: the difference between what the model proposed
    and what we were willing to enforce. It is shown on the review screen and it is the demo
    beat that makes "the model does not decide" visible rather than asserted.
    """

    def __init__(
        self,
        instruction: str,
        catalogue: Mapping[str, Iterable[str]] | None = None,
        places: Iterable[str] | None = None,
    ) -> None:
        self.instruction = instruction
        # Rail 5d: the words the pack's shop cities are written with. None: rail 5d is off.
        self.places: frozenset[str] | None = None if places is None else frozenset(places)
        self.notes: list[str] = []
        self.questions: list[str] = []
        # Rail 5b: per item category, every token of every product name in it. None when the
        # caller has no catalogue, and then rail 5b cannot prove anything and does not fire.
        self.catalogue: dict[str, frozenset[str]] | None = (
            None
            if catalogue is None
            else {
                category: frozenset(t for name in names for t in name_tokens(name))
                for category, names in catalogue.items()
            }
        )
        # Rail 5c: every product name as its own token set, so an excluded phrase can be shown
        # to name at least one real product. None without a catalogue, and then it keeps all.
        self.products: list[frozenset[str]] | None = (
            None
            if catalogue is None
            else [frozenset(name_tokens(name)) for names in catalogue.values() for name in names]
        )

    def note(self, code: str, detail: str = "") -> None:
        self.notes.append(f"{code}: {detail}" if detail else code)

    def ask(self, question: str) -> None:
        if question not in self.questions:
            self.questions.append(question)

    # -------------------------------------------------------------- rules

    def rule(self, raw: Any) -> dict[str, Any] | None:
        """Rail 6 + 2 + 8: a spend cap the enforcement layer can actually read."""
        if not isinstance(raw, dict):
            self.note("invalid_rule", "not an object")
            return None

        field = str(raw.get("field", "billing_amount_chf"))
        operator = str(raw.get("operator", "<="))
        scope = str(raw.get("scope", "purchase"))
        value = _number(raw.get("value"))

        if field not in RULE_FIELDS:
            self.note("invalid_rule", f"field {field!r}")
            return None
        if operator not in RULE_OPERATORS:
            self.note("invalid_rule", f"operator {operator!r}")
            return None
        if scope not in RULE_SCOPES:
            self.note("invalid_rule", f"scope {scope!r}")
            return None
        if value is None or value <= 0:
            self.note("invalid_rule", f"value {raw.get('value')!r}")
            return None
        if not quotes_instruction(raw.get("provenance"), self.instruction):
            self.note("provenance_not_in_instruction", f"rule {value:g} {scope}")
            return None
        converted = self.currency(str(raw["provenance"]), value)
        if converted is None:
            return None
        value, stated = converted

        confidence = self.confidence(raw.get("confidence"))
        rule: dict[str, Any] = {
            "field": field,
            "operator": operator,
            "value": value,
            "currency": "CHF",
            "scope": scope,
            "confidence": confidence,
            "provenance": str(raw["provenance"]),
        }
        if stated is not None:
            rule["stated"] = stated

        if scope == "period":
            days = _number(raw.get("period_days"))
            if days is None or days <= 0:
                # A period cap with no window is not enforceable; the customer states the
                # window, we do not pick one. The baseline's 7-day default is a different
                # thing: there it is the regex's own reading of "any seven days".
                self.note("invalid_rule", "period cap without period_days")
                self.ask("Over how many days should the spending total apply?")
                return None
            rule["period_days"] = int(days)

        # A cap PERMITS spending, so dropping a low-confidence one loosens the mandate below
        # what the customer wrote. Keep it and ask — the opposite of the facet rule below.
        if confidence == "low":
            self.ask(
                f"Did you mean the CHF {value:.2f} limit to apply "
                f"{'to each order' if scope == 'purchase' else 'to your total spending'}?"
            )
        return rule

    def currency(
        self, provenance: str, value: float
    ) -> tuple[float, dict[str, object] | None] | None:
        """Rail 12: a cap written in another currency is enforced as CHF, never as its number.

        The model is handed "Pay no more than EUR 200" and may return 200 in CHF — rail 6
        accepts it, because the quote contains "200". The stated amount is the foreign mention
        in the rule's own provenance whose number is the model's value (the model copied it),
        else the only foreign mention. The enforced value is the lower of the model's and the
        converted one: a model that converted keeps its rule, a model that copied the number
        is corrected, and neither can loosen. None when the rule must be dropped.
        """
        found = mentions(provenance)
        if any(not m.foreign and float(m.amount) == value for m in found):
            return value, None  # the model's number is a CHF amount the customer wrote
        foreign = [m for m in found if m.foreign]
        if not foreign:
            codes = unconvertible(provenance)
            if not codes:
                return value, None
            self.note("currency_not_convertible", f"{codes[0]} in {provenance!r}")
            self.ask(question_for(codes[0]))
            return None
        matching = {(m.amount, m.currency): m for m in foreign if float(m.amount) == value}
        candidates = list(matching.values()) or (foreign if len(foreign) == 1 else [])
        if len(candidates) != 1:
            self.note("currency_ambiguous", f"which amount in {provenance!r} is the limit")
            self.ask(f"Which amount in \u201c{provenance}\u201d is the limit?")
            return None
        stated = candidates[0]
        chf = float(stated.chf())
        if chf < value:
            self.note(
                "currency_converted",
                f"{stated.describe()} → CHF {chf:.2f} at {stated.stated()['rate']}",
            )
        return min(value, chf), stated.stated()

    # -------------------------------------------------------------- facets

    def facet(self, raw: Any) -> dict[str, Any] | None:
        """Rails 2-5, 7-8: an intent facet one of our checks will read."""
        if not isinstance(raw, dict):
            self.note("unknown_facet_kind", "not an object")
            return None

        kind = str(raw.get("kind", ""))
        if kind not in FACET_KINDS:
            self.note("unknown_facet_kind", kind or "(missing)")
            self.ask(
                f"Your instruction asks for something we do not yet enforce ({kind!r}). "
                "Should the agent still buy when it cannot be checked?"
            )
            return None

        if not quotes_instruction(raw.get("provenance"), self.instruction):
            self.note("provenance_not_in_instruction", f"facet {kind}")
            return None

        confidence = self.confidence(raw.get("confidence"))
        require = self.require(kind, raw.get("require"))

        # A facet RESTRICTS, so enforcing a guess over-blocks — the failure mode the challenge
        # calls out. Ask instead. (specs/llm-compiler.md, "Low confidence".)
        if confidence == "low":
            self.note("low_confidence_facet", kind)
            self.ask(
                str(raw.get("open_question") or "")
                or f"Should the agent require {kind.replace('_', ' ')} for this purchase?"
            )
            return None

        # `no_additions` carries no requirement — its presence is the whole rule. Every other
        # kind with nothing left to require would render as enforced and enforce nothing.
        if not require and FACET_REQUIRE_KEYS[kind]:
            self.note("facet_empty_after_guard", kind)
            self.ask(
                "Your instruction rules something out that no product we can check is named "
                "after. Which products or kinds of shop should never be bought?"
                if kind == "category_exclusion"
                else f"Which purchases should count as {kind.replace('_', ' ')}?"
            )
            return None

        facet: dict[str, Any] = {
            "kind": kind,
            "provenance": str(raw["provenance"]),
            "confidence": confidence,
        }
        if require:
            facet["require"] = require
        return facet

    def require(self, kind: str, raw: Any) -> dict[str, Any]:
        """Rails 4-5b: keep only the requirement keys the check for `kind` reads."""
        if not isinstance(raw, dict):
            return {}
        allowed = FACET_REQUIRE_KEYS[kind]
        out: dict[str, Any] = {}
        for key, value in raw.items():
            # Structured outputs in strict mode require EVERY property to be listed in
            # `required`, so a model that means "not stated" has no way to say it except by
            # sending null. A null is silence, not an unrecognised requirement: noting it
            # would fill the review screen's guard trail with entries for things nobody
            # asked for, and asking about it once produced a question with the literal word
            # "None" in it, addressed to a customer.
            if value is None:
                continue
            if key not in allowed:
                self.note("unknown_requirement", f"{kind}.{key}")
                continue
            if key in category_vocabulary():
                kept = self.categories(key, value)
                if kept:
                    out[key] = kept
                continue
            cleaned = self.scalar(kind, key, value)
            if cleaned is not None:
                out[key] = cleaned
        if kind == "item_identity":
            self.satisfiable_keywords(out)
            self.placeless_keywords(out)
        return out

    def placeless_keywords(self, require: dict[str, Any]) -> None:
        """Rail 5d: a place is where a shop is, never a word a product is named with.

        Observed 2026-09-24 on live SCEN0124, "Book me a hotel in Munich…": the model required
        the keyword `munich`. No hotel room is named after a city, so every booking would have
        failed its own identity check, the over-block §A had just fixed. A keyword that is a
        word of a shop city in the pack, and that no catalogue product carries, is dropped, and
        the customer is told the place is not enforced. `kayak` stays: it is not a place, and
        declining a thing the catalogue does not sell is correct (rail 5b's counter-example).
        """
        keywords = require.get("item_keywords_all")
        if not keywords or self.places is None:
            return
        carried: set[str] = set()
        for tokens in (self.catalogue or {}).values():
            carried |= tokens
        kept: list[str] = []
        for word in keywords:
            if word in self.places and word not in carried:
                self.note("keyword_is_a_place", repr(word))
                self.ask(
                    f"Where a shop is cannot be checked yet, so {word.title()!r} is not "
                    "enforced: a shop elsewhere would not be refused for it."
                )
            else:
                kept.append(word)
        if kept:
            require["item_keywords_all"] = kept
        else:
            del require["item_keywords_all"]

    def keywords(self, value: Any) -> list[str] | None:
        """Rail 5a: a keyword is cut into the tokens the item check will compare it with.

        `27-inch` is not a token of any product name — the check splits `27-inch computer
        monitor` into `27`, `inch`, `computer`, `monitor` — so as written it declines every
        monitor. Cut the same way, it asks for exactly the words the model wrote.
        """
        raw = value if isinstance(value, list) else [value]
        words: set[str] = set()
        for entry in raw:
            if entry is None:  # strict-mode padding inside the list
                continue
            text = str(entry).strip()
            tokens = name_tokens(text)
            if tokens != {text.lower()} and text:
                split = ", ".join(sorted(tokens, key=text.lower().find)) or "(nothing)"
                self.note("keyword_tokenised", f"{text!r} → {split}")
            words |= tokens
        return sorted(words) or None

    def excluded_words(self, value: Any) -> list[str] | None:
        """Rail 5c: a product the customer rules out by name must be a product we sell.

        "No alcohol" reaches the check as the words products are named with ("wine",
        "spirits"). Each entry is a phrase, cut by the item check's own tokenizer, with
        function words removed. "wine and spirits" must not become "and", which "Drinks and
        snacks" carries. An entry whose words name no catalogue product excludes nothing and
        only reads as enforced, so it is dropped with a note. If that empties the facet,
        `facet()` asks the customer. specs/llm-compiler.md §"Keywords".
        """
        raw = value if isinstance(value, list) else [value]
        kept: list[str] = []
        for entry in raw:
            if entry is None:  # strict-mode padding inside the list
                continue
            text = str(entry).strip()
            words = sorted(name_tokens(text) - _FUNCTION_WORDS, key=text.lower().find)
            if not words:
                self.note("excluded_word_empty", repr(text))
                continue
            if self.products is not None and not any(set(words) <= p for p in self.products):
                self.note("excluded_word_matches_no_product", repr(text))
                continue
            phrase = " ".join(words)
            if phrase not in kept:
                kept.append(phrase)
        return kept or None

    def weekdays(self, kind: str, value: Any) -> list[str] | None:
        """The days a customer allows, as `mon` … `sun`, or nothing at all.

        One day we cannot read drops the whole list and asks. Keeping the rest would narrow the
        rule ("fri." lost is Friday forbidden) or, read the other way, widen it, and either is
        a guess. specs/check-spending-hours.md §"Days".
        """
        raw = value if isinstance(value, list) else [value]
        days: set[str] = set()
        for entry in raw:
            if entry is None:  # strict-mode padding inside the list
                continue
            text = str(entry).strip().lower()
            day = next((d for d, name in DAY_NAMES.items() if text in (d, name.lower())), None)
            if day is None:
                self.note("unknown_requirement", f"{kind}.weekdays={entry!r}")
                self.ask("On which days of the week may the agent buy?")
                return None
            days.add(day)
        return [d for d in WEEKDAYS if d in days] or None

    def satisfiable_keywords(self, require: dict[str, Any]) -> None:
        """Rail 5b: drop a kind word the category already covers and no product carries.

        All three conditions, each for a catalogue row that would otherwise go wrong
        (specs/llm-compiler.md, "Keywords"): `kayak` is not a kind word, so it keeps declining
        a thing the catalogue does not sell; without a category a kind word is the only
        statement of kind; `fuel` is a kind word that `Fuel purchase` carries, and it is what
        separates fuel from EV charging.
        """
        keywords = require.get("item_keywords_all")
        categories = require.get("item_category_in")
        if not keywords or not categories or self.catalogue is None:
            return
        carried: set[str] = set()
        for category in categories:
            carried |= self.catalogue.get(category, frozenset())
        category_words = _category_words()
        kept: list[str] = []
        for word in keywords:
            if word in category_words and word not in carried:
                self.note(
                    "keyword_matches_no_product",
                    f"{word!r} (no {' or '.join(categories)} product has it in its name)",
                )
            else:
                kept.append(word)
        if kept:
            require["item_keywords_all"] = kept
        else:
            del require["item_keywords_all"]

    def categories(self, key: str, value: Any) -> list[str]:
        """Rail 5: a category the data pack does not contain can never match anything."""
        vocabulary = category_vocabulary()[key]
        raw = value if isinstance(value, list) else [value]
        kept: list[str] = []
        for candidate in raw:
            if candidate is None:  # strict-mode padding inside the list, same as above
                continue
            name = str(candidate).strip().lower()
            if name in vocabulary:
                kept.append(name)
            else:
                self.note("unknown_category", f"{key}={candidate!r}")
                self.ask(
                    f"Your instruction mentions {str(candidate).replace('_', ' ')!r}, "
                    "which is not one of the shop or product categories we can check. "
                    "Which of the ones we do have did you mean?"
                )
        return sorted(set(kept))

    def scalar(self, kind: str, key: str, value: Any) -> Any:
        """Non-category requirement values: numbers stay numbers, keywords stay lowercase."""
        if key in ("size", "prior_approvals_min", "return_window_days_min"):
            number = _number(value)
            if number is None or number < 0:
                self.note("unknown_requirement", f"{kind}.{key}={value!r}")
                return None
            return int(number)
        if key == "item_keywords_all":
            return self.keywords(value)
        if key == "item_keywords_none":
            return self.excluded_words(value)
        if key in ("hours_from", "hours_to"):
            hour = _number(value)
            if hour is None or not hour.is_integer() or not 0 <= hour <= 23:
                self.note("unknown_requirement", f"{kind}.{key}={value!r}")
                return None
            return int(hour)
        if key == "weekdays":
            return self.weekdays(kind, value)
        if key == "item_description":
            text = str(value).strip()
            return text or None
        if key == "cancellable":
            # Only `true` means anything: "refundable rate only". A `false` would say "only
            # non-refundable", which no customer asks for, so it is dropped rather than read
            # as a rule. specs/check-order-terms.md §"Cancellation".
            if value is True or str(value).strip().lower() == "true":
                return True
            self.note("unknown_requirement", f"{kind}.{key}={value!r}")
            return None
        return value

    # -------------------------------------------------------------- scalars

    def confidence(self, value: Any) -> str:
        """Rail 7. An absent or unrecognised confidence is `low` — the cautious reading."""
        level = str(value).strip().lower()
        return level if level in CONFIDENCE_LEVELS else "low"

    def uncertainty_policy(self, value: Any) -> str:
        """Rail 10. Anything we do not recognise means `ask`, never `approve`."""
        policy = str(value).strip().lower()
        if policy in UNCERTAINTY_POLICIES:
            return policy
        if value is not None:
            self.note("invalid_uncertainty_policy", repr(value))
        return "ask"

    def strings(self, value: Any, limit: int = 12) -> list[str]:
        """Guidance and open questions: plain sentences, deduplicated, bounded."""
        raw = value if isinstance(value, list) else []
        out: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out[:limit]


def accept(
    raw: Any,
    instruction: str,
    *,
    model: str,
    catalogue: Mapping[str, Iterable[str]] | None = None,
    places: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Turn one model response into a Policy IR, or None when nothing survived.

    None means "fall back to the baseline". Returning a thin IR instead would be worse: a
    mandate that looks compiled and enforces nothing is the one outcome nobody can reason
    about (specs/llm-compiler.md, "Fallback" — the fallback is total, not partial).
    """
    if not isinstance(raw, dict):
        return None

    guard = Guard(instruction, catalogue, places)

    rules: list[dict[str, Any]] = []
    for candidate in raw.get("rules") or ():
        rule = guard.rule(candidate)
        if rule is not None and rule not in rules:
            rules.append(rule)

    # Rail 11: domain/policy.facet() reads the FIRST facet of a kind, so a second one of the
    # same kind is not a second requirement — it is a requirement that silently does nothing.
    # Three kinds only ever add restrictions, so a second one is merged tightest-wins, the way
    # composition merges layers. A model writes "No flights, no insurance" as two exclusions,
    # and dropping the second lost "no insurance" on live SCEN0124. Any other kind, or a merge
    # that would conflict, keeps the first and notes the rest, as before.
    facets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in raw.get("intent_facets") or ():
        facet = guard.facet(candidate)
        if facet is None:
            continue
        if facet["kind"] in seen:
            if facet["kind"] in _MERGED_KINDS:
                index = next(i for i, f in enumerate(facets) if f["kind"] == facet["kind"])
                merged, conflict = merge_facets(facet["kind"], [facets[index], facet])
                if merged is not None and conflict is None:
                    facets[index] = merged
                    guard.note("facet_merged", facet["kind"])
                    continue
            guard.note("duplicate_facet", facet["kind"])
            continue
        seen.add(facet["kind"])
        facets.append(facet)

    if not rules and not facets:
        return None

    questions = guard.strings(raw.get("open_questions"))
    for question in guard.questions:
        if question not in questions:
            questions.append(question)

    # `guidance` is the plain-language account the customer reads before consenting. The
    # rest of this module checks that nothing UNJUSTIFIED is enforced; this checks the other
    # direction — that everything enforced is also explained. Observed 2026-09-09: a
    # five-requirement mandate whose guidance mentioned only the spend cap.
    #
    # Informational, not a rejection. One good sentence can legitimately cover two related
    # requirements, so a short count is a reason to read the review screen, not a defect.
    guidance = guard.strings(raw.get("guidance"))
    enforced = len(rules) + len(facets)
    if len(guidance) < enforced:
        guard.note(
            "guidance_thinner_than_policy", f"{enforced} enforced, {len(guidance)} explained"
        )

    return {
        "source_instruction": instruction,  # rail 1: never the model's copy of it
        "instruction_sha256": instruction_hash(instruction),
        "uncertainty_policy": guard.uncertainty_policy(raw.get("uncertainty_policy")),
        "rules": rules,
        "intent_facets": facets,
        "guidance": guidance,
        "open_questions": questions,
        "compiler": f"llm:{model}",
        "compiler_notes": guard.notes,
    }


def apply_safety_floor(ir: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Rail 9: the model may re-scope or tighten a cap, never lose it or loosen it.

    What plain-text extraction actually proves is the **magnitude**: "CHF 120 is a cap in this
    instruction". It does *not* prove the scope — deciding whether "never more than CHF 75 on
    a single order" caps the order or the period is exactly the judgement the regex is bad at
    and the model is good at. So the floor matches on value, not on scope: every cap the
    baseline found must be covered by an accepted cap of no more than that amount, one for
    one. Uncovered caps are adopted from the baseline.

    Matching both sides in ascending order is optimal here: caps live on a number line, so
    covering the smallest baseline cap with the smallest sufficient accepted cap never strands
    a larger one. Multiplicity matters — two baseline caps cannot both be covered by one
    accepted rule, or a model that dropped the weekly total would pass on the per-order cap.

    The claim this buys, in one sentence: *every limit written in your instruction is enforced
    at no more than the amount you wrote.* It holds without trusting the model at all.
    """
    rules = list(ir.get("rules") or ())
    notes = list(ir.get("compiler_notes") or ())

    floors = sorted(
        (rule for rule in baseline.get("rules") or () if _number(rule.get("value")) is not None),
        key=lambda r: _number(r.get("value")) or 0.0,
    )
    accepted = sorted(
        ((i, v) for i, r in enumerate(rules) if (v := _number(r.get("value"))) is not None),
        key=lambda pair: pair[1],
    )

    used: set[int] = set()
    adopted: list[float] = []
    for floor in floors:
        limit = _number(floor.get("value")) or 0.0
        cover = next((i for i, v in accepted if i not in used and v <= limit), None)
        if cover is not None:
            used.add(cover)
            continue
        rules.append({**floor, "confidence": "high", "safety_floor": True})
        adopted.append(limit)
        notes.append(
            f"safety_floor_applied: CHF {limit:.2f} "
            f"({floor.get('scope', 'purchase')}) is stated in the instruction but the model "
            "carried no cap that tight; the deterministic rule was adopted"
        )

    # A cap the model proposed that covered nothing and is looser than a cap we had to adopt
    # is not merely redundant — the evaluator would enforce the tighter one anyway — it is
    # *misleading*, and this IR is a review screen. A customer told "each order stays at or
    # below CHF 2000" directly above "…at or below CHF 200" cannot check either.
    if adopted:
        ceiling = min(adopted)
        dropped = [i for i, v in accepted if i not in used and v > ceiling]
        for index in sorted(dropped, reverse=True):
            value = _number(rules[index].get("value")) or 0.0
            notes.append(f"unsupported_cap_removed: CHF {value:.2f} was not in the instruction")
            del rules[index]

    return {**ir, "rules": rules, "compiler_notes": notes}
