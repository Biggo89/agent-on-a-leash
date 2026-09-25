"""Deterministic instruction compiler — the mandatory fallback.

The LLM compiler (compile/llm.py, hackathon work) produces a richer Policy IR. This one runs
with no model, no network and no key, and must always produce a usable mandate: the challenge
requires the solution to stay predictable when optional models or external services fail.

It is generic regex over the instruction text, NOT per-scenario logic. Hardcoding to a
scenario is explicitly prohibited — if you find yourself matching a scenario_id here, stop.

What it cannot express becomes an open_question for the customer, which is the honest
behaviour: "clearly highlight any uncertainty".
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.settings import item_categories
from .currency import Mention, mentions, question_for, unconvertible

_PERIOD = re.compile(r"across\s+any\s+(\w+)\s+days?", re.IGNORECASE)
# Per-order cue. "on a single order" is the phrasing that exposed the gap: without it the
# amount takes the nearest *period* cue instead and the per-order cap is scoped as a window
# total — which, under decision-rules.md §9, then loses to the looser period cap and is
# dropped entirely. Every alternative needs a quantifier, so the bare noun never matches.
_PER_ORDER = re.compile(
    r"\b(?:each|every|per|one|(?:a|any)\s+single)\s+(?:order|purchase|transaction)\b",
    re.IGNORECASE,
)
_TOTAL = re.compile(r"\btotal\b", re.IGNORECASE)
# `uncertainty_policy` — policy-ir.md compiler rule 6. Only the strict direction is
# extracted: `ask` is the default, so an explicit "ask me when uncertain" needs no pattern,
# and `approve` is deliberately unreachable from here — a wrongly-matched loose cue spends
# money on a guess, where a missed strict cue only asks a question.
#
# The conditional is what carries the meaning, so the verb alone never matches: "Decline
# anything over CHF 100" is about a thing, not about uncertainty. Both clause orders are
# accepted, and neither alternative may cross a sentence boundary.
_UNSURE = r"(?:you\s+are\s+|you're\s+|it\s+is\s+)?(?:not\s+sure|unsure|uncertain|in\s+doubt)"
_UNCERTAIN_DECLINE = re.compile(
    rf"\b(?:if|when|whenever)\s+{_UNSURE}\s*,?\s*(?:then\s+)?decline\b"
    rf"|\bdecline\b\s*(?:it\s+)?(?:if|when|whenever)\s+{_UNSURE}",
    re.IGNORECASE,
)
_RETURN_DAYS = re.compile(r"returned?\s+within\s+(\w+)\s+days?\s*(or more)?", re.IGNORECASE)
# A price per unit, right after the amount: "at most CHF 200 per night". Not an order cap: a
# three-night stay at CHF 190 a night bills CHF 570, and reading "CHF 200" as the order cap
# declined every booking live SCEN0124 asked for (specs/llm-compiler.md §"Derived caps").
# Nights only, the one unit a live instruction uses. Another unit is a spec change first.
_PER_UNIT = re.compile(r"\s*(?:per|a|an|each)\s+(night)\b", re.IGNORECASE)
# How many units the order covers: "for 3 nights". The count must be a number or a number
# word; "per night" itself matches the shape and is discarded by `_int`.
_UNIT_COUNT = {"night": re.compile(r"\b(\w+)\s+nights?\b", re.IGNORECASE)}
# "refundable rate only", "free cancellation". The platform carries the fact as
# `order_cancellable` (specs/check-order-terms.md §"Cancellation"). The lookbehind keeps
# "non-refundable" and "nonrefundable" out; `_NEGATION_BEFORE` keeps out "not refundable".
_REFUNDABLE = re.compile(
    r"(?<![\w-])(?:(?:only\s+)?(?:fully\s+)?refundable"
    r"(?:\s+(?:rates?|bookings?|fares?|tickets?|options?))?(?:\s+only)?"
    r"|free\s+cancell?ation|(?:fully\s+)?cancell?able)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE = re.compile(r"\b(?:not|never|no|without)\s+$", re.IGNORECASE)
# "Never at the weekend", "not on weekends", "weekdays only", "no weekend deliveries": the days
# the agent may buy on, read in Swiss time (specs/check-spending-hours.md §"Days"). A bare
# "weekend" is not a rule: "book a weekend getaway" names a trip. So each alternative needs
# a negation that ends the clause, a noun that makes it an order, or "only".
_NO_WEEKEND = re.compile(
    r"\b(?:never|not)\s+(?:on|at|during|over)?\s*(?:the\s+|a\s+)?weekends?\b(?=\s*(?:[.,;!]|$))"
    r"|\bno\s+weekend\s+(?:orders?|deliver(?:y|ies)|purchases?|shopping|buying)\b"
    r"|\bweekdays?\s+only\b|\bonly\s+on\s+weekdays\b",
    re.IGNORECASE,
)
# A meal used as a restriction names a time of day without stating one: "weeknight dinners
# only". Not a rule, never an hours window: the customer is asked (specs/live-instructions.md
# §C). A meal that is the thing bought ("buy lunch for the team") is not a time at all.
_MEAL = r"(dinners?|lunch(?:es)?|breakfasts?)"
_MEAL_TIME = re.compile(
    rf"\b(?:weeknight|weekday|evening|nightly|daily)\s+{_MEAL}\b|\b{_MEAL}\s+only\b",
    re.IGNORECASE,
)
_FAMILIAR = re.compile(
    r"(?:shops?|sellers?|stores?)\s+I\s+(?:have\s+)?(?:used|bought from)\s+before", re.IGNORECASE
)
_NO_ADDITIONS = re.compile(r"do\s+not\s+add\s+anything", re.IGNORECASE)
# "from a specialist sports retailer" — a requirement about the KIND of shop. Deliberately
# needs an article, so "from shops I have used before" and "from a seller I have bought from
# before" (both familiarity, not type) do not match.
# "avoids gift vouchers" / "never buy gift cards" / "no gift vouchers".
#
# Deliberately narrow. The cue alone proves nothing — "no more than CHF 200" and "do not add
# anything I did not ask for" both contain a negation and neither excludes a category — so a
# facet is emitted ONLY when the captured phrase maps to a category in the pack's vocabulary.
# A phrase that maps to nothing produces nothing, which is why this cannot fire on the five
# pack instructions. `make diff-decisions` is the proof, not this comment.
_EXCLUDES = re.compile(
    r"\b(?:avoids?|never\s+buys?|no)\s+([a-z][a-z\s-]{1,40}?)(?=\s*[.,;]|\s*$)",
    re.IGNORECASE,
)

_RETAILER_TYPE = re.compile(
    r"from\s+(?:a|an|the)\s+([a-z][a-z\s-]{1,40}?)\s+"
    r"(?:retailer|shop|store|seller|merchant|dealer)s?\b",
    re.IGNORECASE,
)

# The object of the buy verb: "Replace my worn road-running shoes in size 43" -> the phrase.
_BUY_OBJECT = re.compile(
    r"\b(?:buy|order|purchase|replace|get)\s+"
    r"((?:(?!\b(?:for|from|up\s+to|in\s+size|only|and|when|,)\b)[\w'-]+\s*){1,6})",
    re.IGNORECASE,
)
# A hyphenated or numeric-qualified compound is a product specification ("road-running",
# "27-inch"). A loose adjective ("household") is not, and treating it as one would decline
# every SCEN0001 grocery basket, since no item is literally named "household".
_COMPOUND = re.compile(r"\b(\w+-\w+)\b")
# "in size 43". Numeric only: the pack also has letter sizes (S/M/L), but no instruction
# ever requests one, so both sides ignore them consistently.
_ITEM_SIZE = re.compile(r"\bsize\s+(\d{1,3})\b", re.IGNORECASE)
# Determiners and non-product adjectives that describe the request, not the product.
_ITEM_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "our",
        "me",
        "i",
        "one",
        "some",
        "new",
        "worn",
        "ordinary",
        "chose",
        "chosen",
        "item",
        "items",
        "please",
        "of",
        "his",
        "her",
        "their",
        "us",
    }
)

# Natural language -> the pack's item_category vocabulary. Separate from the merchant map:
# gift_card, cosmetics and membership are item categories with no merchant counterpart.
ITEM_CATEGORY_VOCABULARY: dict[str, tuple[str, ...]] = {
    "sporting_goods": ("shoe", "shoes", "trainers", "sneakers", "helmet", "sports", "sport"),
    "electronics": ("monitor", "screen", "computer", "laptop", "phone", "tv", "electronics"),
    "clothing": ("clothing", "clothes", "jacket", "coat", "trousers", "shirt", "outerwear"),
    "groceries": ("groceries", "grocery", "food", "produce", "supermarket"),
    # "gift card" arrives as one word: `_GIFT_PHRASES` joins the two before this map is read,
    # so the bare word "card" or "gift" never names a category on its own.
    "gift_card": (
        "voucher",
        "vouchers",
        "giftcard",
        "giftcards",
        "giftvoucher",
        "giftvouchers",
        "giftcertificate",
        "giftcertificates",
    ),
    "cosmetics": ("cosmetics", "fragrance", "perfume", "beauty"),
    "books": ("book", "books"),
    "household": ("household",),
    # Item categories only the live pack uses (2026-09-24): the SCEN0122 camera lens is
    # `photography`, flights and travel insurance are `travel`. Mapped only while the loaded
    # pack has them (`_item_categories_for`), so the repo pack compiles exactly as before.
    "photography": ("camera", "lens", "photography"),
    "travel": ("flight", "flights", "insurance"),
}


# A product a customer rules out that is not a category of its own. The live pack files "Wine
# and spirits" (IT0168) under `groceries`, so "No alcohol" names no category, and nothing
# excluded wine: live SCEN0117 approved it. Concept word -> the words products are named with,
# matched on the item's catalogue name (specs/check-category-exclusion.md §"Product words").
# Written down, small, and grown by review, never by a model: the baseline must enforce "no
# alcohol" without one. `drinks` is deliberately absent: "Drinks and snacks" is soft drinks.
EXCLUSION_LEXICON: dict[str, tuple[str, ...]] = {
    "alcohol": ("wine", "spirits", "beer", "liquor"),
    "alcoholic": ("wine", "spirits", "beer", "liquor"),
}
_GIFT_PHRASES = re.compile(r"\bgift\s+(cards?|vouchers?|certificates?)\b", re.IGNORECASE)


def _item_categories_for(words: set[str]) -> list[str]:
    """The item categories these words name, among those the loaded pack actually has."""
    known = item_categories()
    return sorted(
        cat
        for cat, terms in ITEM_CATEGORY_VOCABULARY.items()
        if cat in known and words & set(terms)
    )


# Natural language -> the pack's shared merchant_category vocabulary. Generic terms only;
# this is a language map, never a scenario lookup.
CATEGORY_VOCABULARY: dict[str, tuple[str, ...]] = {
    "sporting_goods": ("sport", "sports", "sporting", "athletic", "outdoor", "running"),
    "groceries": ("grocery", "groceries", "supermarket", "food"),
    "electronics": ("electronics", "electronic", "computer", "tech", "technology"),
    "clothing": ("clothing", "clothes", "fashion", "apparel", "outfitter"),
    "books": ("book", "books", "bookshop", "bookstore"),
    "health": ("pharmacy", "chemist", "health", "drugstore"),
    "pet_care": ("pet", "pets"),
    "home_improvement": ("hardware", "diy", "homeware"),
    "kids_family": ("toy", "toys", "children", "kids"),
    "photography": ("photography", "camera"),
    "sustainable_goods": ("sustainable", "secondhand", "refurbished"),
}

_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fourteen": 14,
    "thirty": 30,
}


def _int(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORDS.get(token)


def compile_instruction(instruction: str) -> dict[str, Any]:
    """Return a Policy IR dict. The instruction itself is carried through verbatim."""
    rules: list[dict[str, Any]] = []
    facets: list[dict[str, Any]] = []
    guidance: list[str] = []
    questions: list[str] = []

    # "CHF 120", "at or below CHF 300", "Pay no more than EUR 200": every amount the pack can
    # convert, as CHF (currency.py). One it cannot convert is a question, never a guessed rule.
    amounts = mentions(instruction)
    for code in unconvertible(instruction):
        questions.append(question_for(code))
    period = _PERIOD.search(instruction)
    period_days = _int(period.group(1)) if period else None

    # Scope each amount by its NEAREST cue in either direction. "each order at or below
    # CHF 120" puts the cue before the amount; "up to CHF 250 per order" puts it after.
    purchase_cues = [m.start() for m in _PER_ORDER.finditer(instruction)]
    period_cues = [m.start() for m in _TOTAL.finditer(instruction)]
    if period:
        period_cues.append(period.start())

    def _nearest(cues: list[int], pos: int) -> int | None:
        return min((abs(c - pos) for c in cues), default=None)

    for mention in amounts:
        # "CHF 200 per night" with "3 nights" quoted: the order cap is their product, and the
        # rule quotes both spans (specs/llm-compiler.md §"Derived caps"). Without a single
        # count the words give no total, so today's per-order reading stands and we ask.
        if per_unit := _PER_UNIT.match(instruction, mention.start + len(mention.text)):
            unit = per_unit.group(1).lower()
            counted = [
                (n, m.group(0))
                for m in _UNIT_COUNT[unit].finditer(instruction)
                if (n := _int(m.group(1)))
            ]
            if len({n for n, _ in counted}) == 1:
                count, count_quote = counted[0]
                rules.append(_derived_cap(mention, per_unit, instruction, count, count_quote))
                total = mention.times(count)
                guidance.append(
                    f"Each order stays at or below CHF {float(total.chf()):.2f} "
                    f"({count} {unit}s × {mention.describe()} per {unit})."
                )
                continue
            questions.append(f"Your limit is per {unit}. How many {unit}s may one order cover?")

        value = float(mention.chf())
        to_purchase = _nearest(purchase_cues, mention.start)
        to_period = _nearest(period_cues, mention.start)
        # No cue at all defaults to a per-purchase cap: the tighter reading of an ambiguous
        # instruction, and the one a cardholder is least surprised by.
        if to_period is None:
            scope = "purchase"
        elif to_purchase is None:
            scope = "period"
        else:
            scope = "period" if to_period < to_purchase else "purchase"

        rule: dict[str, Any] = {
            "field": "billing_amount_chf",
            "operator": "<=",
            "value": value,
            "currency": "CHF",
            "scope": scope,
            # policy-ir.md rule 2: every rule quotes the instruction it came from, so the
            # customer can check the compilation. The regex knows the exact span it matched;
            # scope is inferred from nearby cues, so it is `medium`, not `high`.
            "provenance": mention.text,
            "confidence": "medium",
        }
        # policy-ir.md compiler rule 7: the value is CHF; `stated` keeps what was written.
        if mention.foreign:
            rule["stated"] = mention.stated()
        written = f" ({mention.describe()})" if mention.foreign else ""
        if scope == "period":
            rule["period_days"] = period_days or 7
            if period_days is None:
                questions.append("Over how many days should the spending total apply?")
        if rule not in rules:
            rules.append(rule)
            guidance.append(
                f"Each order stays at or below CHF {value:.2f}{written}."
                if scope == "purchase"
                else (
                    f"Total spending stays at or below CHF {value:.2f}{written} "
                    f"across {rule['period_days']} days."
                )
            )

    if _PER_ORDER.search(instruction) and not any(r["scope"] == "purchase" for r in rules):
        questions.append("What is the per-order limit?")

    # One order_terms facet for every term stated, because the checks read the first facet of
    # a kind (domain/policy.py). Each term keeps its own quote.
    order_terms: dict[str, Any] = {}
    order_quotes: list[str] = []
    if (m := _RETURN_DAYS.search(instruction)) and (days := _int(m.group(1))):
        order_terms["return_window_days_min"] = days
        order_quotes.append(m.group(0))
        guidance.append(f"Only orders returnable within at least {days} days will be bought.")
    refundable = next(
        (
            m
            for m in _REFUNDABLE.finditer(instruction)
            if not _NEGATION_BEFORE.search(instruction[: m.start()])
        ),
        None,
    )
    if refundable is not None:
        order_terms["cancellable"] = True
        order_quotes.append(refundable.group(0).strip())
        guidance.append("Only orders that can be cancelled for a refund will be bought.")
    if order_terms:
        facets.append(
            {
                "kind": "order_terms",
                "require": order_terms,
                "provenance": _quoted(order_quotes),
                "confidence": "high",
            }
        )

    if m := _NO_WEEKEND.search(instruction):
        facets.append(
            {
                "kind": "spending_hours",
                "require": {"weekdays": ["mon", "tue", "wed", "thu", "fri"]},
                "provenance": m.group(0).strip(),
                "confidence": "high",
            }
        )
        guidance.append("Nothing will be bought on a Saturday or Sunday (Swiss time).")

    if meal := _MEAL_TIME.search(instruction):
        word = (meal.group(1) or meal.group(2)).lower()
        questions.append(
            f"What hours count as {word!r}? Until you say, the agent does not limit the time "
            "of day."
        )

    if _FAMILIAR.search(instruction):
        facets.append(
            {
                "kind": "merchant_familiarity",
                "require": {"prior_approvals_min": 1},
                "provenance": _FAMILIAR.search(instruction).group(0),  # type: ignore[union-attr]
                "confidence": "high",
            }
        )
        guidance.append("Only merchants you have bought from before will be used.")

    if _NO_ADDITIONS.search(instruction):
        facets.append(
            {
                "kind": "no_additions",
                "provenance": _NO_ADDITIONS.search(instruction).group(0),  # type: ignore[union-attr]
                "confidence": "high",
            }
        )
        guidance.append("Nothing beyond what you asked for will be added to the order.")

    if m := _ITEM_SIZE.search(instruction):
        facets.append(
            {
                "kind": "item_attribute",
                "require": {"size": int(m.group(1))},
                "provenance": m.group(0),
                "confidence": "high",
            }
        )
        guidance.append(f"Only size {m.group(1)} will be bought.")

    # Every "no X" the words can back, in ONE facet: the checks read the first facet of a kind
    # (domain/policy.py), so a second would be inert. Stopping at the first match lost the
    # rest: live SCEN0117's "No alcohol, no gift cards, no cosmetics" kept only cosmetics.
    excluded_categories: set[str] = set()
    excluded_words: list[str] = []
    exclusion_quotes: list[str] = []
    for m in _EXCLUDES.finditer(instruction):
        phrase = _GIFT_PHRASES.sub(lambda g: "gift" + g.group(1).lower(), m.group(1).strip())
        words = set(re.findall(r"[a-z]+", phrase.lower()))
        categories = _item_categories_for(words)
        named = [
            w
            for concept in sorted(words & set(EXCLUSION_LEXICON))
            for w in EXCLUSION_LEXICON[concept]
        ]
        if not categories and not named:
            continue  # a negation with nothing behind it is not an exclusion
        excluded_categories.update(categories)
        excluded_words.extend(w for w in named if w not in excluded_words)
        exclusion_quotes.append(m.group(0).strip())
    if exclusion_quotes:
        exclusion: dict[str, Any] = {}
        if excluded_categories:
            exclusion["item_category_not_in"] = sorted(excluded_categories)
            guidance.append(
                "Nothing "
                + " or ".join(c.replace("_", " ") for c in sorted(excluded_categories))
                + " will be bought."
            )
        if excluded_words:
            exclusion["item_keywords_none"] = excluded_words
            guidance.append(
                "Nothing named "
                + ", ".join(excluded_words[:-1])
                + (" or " if len(excluded_words) > 1 else "")
                + excluded_words[-1]
                + " will be bought."
            )
        facets.append(
            {
                "kind": "category_exclusion",
                "require": exclusion,
                "provenance": _quoted(exclusion_quotes),
                "confidence": "high",
            }
        )

    if m := _RETAILER_TYPE.search(instruction):
        phrase = m.group(1).strip().lower()
        words = set(re.findall(r"[a-z]+", phrase))
        matched = [cat for cat, terms in CATEGORY_VOCABULARY.items() if words & set(terms)]
        if matched:
            facets.append(
                {
                    "kind": "merchant_type",
                    "require": {"merchant_category_in": sorted(matched)},
                    "provenance": m.group(0),
                    # "specialist" is a judgement the customer, not the compiler, resolves.
                    "confidence": "medium",
                }
            )
            guidance.append(
                "Only "
                + " or ".join(c.replace("_", " ") for c in sorted(matched))
                + " shops will be used."
            )
            questions.append(
                f"Does {phrase!r} include a shop in another category that also sells "
                "what you asked for?"
            )
        else:
            # Guessing a category the customer never stated would invent a rule. Ask instead.
            questions.append(f"Which kind of shop counts as {phrase!r}?")

    if m := _BUY_OBJECT.search(instruction):
        descriptor = m.group(1).strip()
        descriptor_words = re.findall(r"[\w'-]+", descriptor.lower())
        content = [w for w in descriptor_words if w not in _ITEM_STOPWORDS]
        # A token can name a category ("shoes") without being distinctive; only compounds
        # ("road-running", "27-inch") pin down which product within that category.
        plain = {w for word in content for w in re.findall(r"[a-z0-9]+", word)}
        categories = _item_categories_for(plain)
        keywords = sorted(
            {
                tok
                for compound in _COMPOUND.findall(descriptor.lower())
                for tok in re.findall(r"[a-z0-9]+", compound)
            }
        )
        if categories or keywords:
            require: dict[str, Any] = {}
            if categories:
                require["item_category_in"] = categories
            if keywords:
                require["item_keywords_all"] = keywords
            require["item_description"] = " ".join(content) or descriptor
            facets.append(
                {
                    "kind": "item_identity",
                    "require": require,
                    "provenance": m.group(0).strip(),
                    "confidence": "high" if keywords else "medium",
                }
            )
            guidance.append(f"Only {require['item_description']} will be bought.")
        else:
            # Guessing what the customer meant to buy would invent a rule. Ask instead.
            questions.append(f"Which items count as {descriptor!r}?")

    return {
        "source_instruction": instruction,
        "uncertainty_policy": "decline" if _UNCERTAIN_DECLINE.search(instruction) else "ask",
        "rules": rules,
        "intent_facets": facets,
        "guidance": guidance,
        "open_questions": questions,
        "compiler": "baseline-deterministic",
    }


def _quoted(quotes: list[str]) -> str | list[dict[str, str]]:
    """One span as the plain string every existing mandate carries; several as a list.

    A bare string reads as `instruction` (domain/provenance.py), so the repo pack's facets are
    unchanged. A facet or rule built from two spans quotes both, and the demo highlights both
    in the customer's own sentence.
    """
    if len(quotes) == 1:
        return quotes[0]
    return [{"source": "instruction", "quote": quote} for quote in quotes]


def _derived_cap(
    mention: Mention, per_unit: re.Match[str], instruction: str, count: int, count_quote: str
) -> dict[str, Any]:
    """The per-order cap a price per unit and a quoted count state together.

    `derived` records the arithmetic for the review screen and the customer message. It never
    reaches the platform: the wire carries field, operator, value, currency, scope and
    period_days only (`to_mandate_payload`, and the schema forbids anything else).
    """
    total = mention.times(count)
    rule: dict[str, Any] = {
        "field": "billing_amount_chf",
        "operator": "<=",
        "value": float(total.chf()),
        "currency": "CHF",
        "scope": "purchase",
        "provenance": _quoted([instruction[mention.start : per_unit.end()], count_quote]),
        "confidence": "medium",
        "derived": {
            "unit": per_unit.group(1).lower(),
            "count": count,
            "unit_amount": float(mention.amount),
            "unit_currency": mention.currency,
        },
    }
    if total.foreign:
        rule["stated"] = total.stated()
    return rule


def to_mandate_payload(ir: dict[str, Any]) -> dict[str, Any]:
    """Policy IR -> POST /v1/mandates body. The instruction must go through byte-identical."""
    return {
        "instruction": ir["source_instruction"],
        "hard_rules": [
            {
                k: v
                for k, v in rule.items()
                if k in ("field", "operator", "value", "currency", "scope", "period_days")
            }
            for rule in ir["rules"]
        ],
        "uncertainty_policy": ir["uncertainty_policy"],
        "guidance": ir["guidance"],
        "open_questions": ir["open_questions"],
    }
