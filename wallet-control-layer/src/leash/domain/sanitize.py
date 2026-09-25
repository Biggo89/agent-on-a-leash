"""Untrusted merchant text: extract facts, never obey instructions.

`item_details`, `merchant_name` and `purchase_description` are merchant-supplied. They carry
facts a decision genuinely needs (a return window exists nowhere else in the event) and, in
SCEN0004, instructions aimed at an automated purchasing system.

The distinction this module enforces:

    Extracting a *fact* from merchant text is legitimate.
    Obeying an *imperative* in it is not.

Every extractor returns a value of a fixed type or None. An extractor that can only emit an
integer cannot be argued into emitting a verdict. Detection results are evidence only: a hit
never changes a limit, a list, or an outcome.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Final

# Imperative / false-authority framing aimed at an automated system.
#
# **Four languages.** This is a control layer for a Swiss issuer: a merchant writes in German,
# French or Italian as readily as in English, and a detector that only reads English is a
# detector that does not work here. The *label* vocabulary is language-neutral and closed —
# adding a language adds patterns, never a reason code — so nothing downstream moves.
#
# Accents are matched both ways (`pr[ée]-?autoris`, `imm[ée]diatement`) rather than folded,
# because `Manipulation.span` indexes the original text: the demo highlights the offending
# words inside the seller's own copy, and folding would slide every offset.
#
# **A false positive is not free.** It puts an accusation in front of a customer about a
# seller that did nothing wrong. Every pattern here is answerable to
# `tests/vectors/injection_corpus.yaml`, which carries ordinary merchant copy in all four
# languages that must NOT match — including the near misses that tempted a looser pattern.
MANIPULATION_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    # — the agent is told to discard what it was told ———————————————————————
    (
        r"ignore\s+(?:any\s+|all\s+)?(?:previous|prior|earlier)\s+\w*\s*instructions",
        "override_instructions",
    ),
    (
        r"\b(?:ignorier\w*|missachte\w*)\b[^.;]{0,40}\b(?:vorherig\w*|bisherig\w*|"
        r"fr[üu]her\w*|vorhergehend\w*)\s+(?:anweisung\w*|vorgab\w*|anordnung\w*)",
        "override_instructions",
    ),
    (
        r"\bignore[rz]?\b[^.;]{0,50}\b(?:instructions|consignes)\s+"
        r"(?:pr[ée]c[ée]dentes|ant[ée]rieures)",
        "override_instructions",
    ),
    (
        r"\bignora(?:re|te)?\b[^.;]{0,50}\b(?:istruzioni|indicazioni)\s+"
        r"(?:precedenti|anteriori)",
        "override_instructions",
    ),
    # — text dressed up as a system turn ———————————————————————————————————
    (r"^\s*(?:system|syst[èe]me|sistema)\s*:", "fake_system_prompt"),
    # — copy addressed to a machine rather than a buyer ——————————————————————
    (r"\bnote\s+for\s+(?:automated|ai|purchasing)\b", "addressed_to_agent"),
    (
        r"\b(?:hinweis|anmerkung|achtung)\s+f[üu]r\s+"
        r"(?:ki|ai|automatische|automatisierte|einkaufs)",
        "addressed_to_agent",
    ),
    (r"\b(?:note|avis|message)\s+(?:pour|aux)\s+(?:les\s+)?agents?\b", "addressed_to_agent"),
    (r"\b(?:nota|avviso|messaggio)\s+per\s+(?:gli\s+)?agenti\b", "addressed_to_agent"),
    # — a claim that consent already exists ————————————————————————————————
    (r"\bpre-?author(?:ised|ized)\b", "claimed_preauthorization"),
    (
        r"\bvorautorisiert\b|\b(?:vorab|vorher|im\s+voraus)\s+autorisiert\b",
        "claimed_preauthorization",
    ),
    (
        r"\bpr[ée]-?autoris[ée]\w*\b|\bautoris[ée]\w*\s+au\s+pr[ée]alable\b",
        "claimed_preauthorization",
    ),
    (
        r"\bpre-?autorizzat\w+\b|\bautorizzat\w+\s+in\s+anticipo\b",
        "claimed_preauthorization",
    ),
    # — a claim that the cardholder's own limits are void ————————————————————
    (r"\blimits?\s+(?:do\s+not|don'?t)\s+apply\b", "claimed_limit_exemption"),
    (
        r"\b(?:limits?|limite|beschr[äa]nkungen|obergrenzen)\s+gelten\s+(?:hier\s+)?nicht\b",
        "claimed_limit_exemption",
    ),
    (r"\blimites?\s+ne\s+s['’]appliquent\s+pas\b", "claimed_limit_exemption"),
    (r"\blimiti\s+non\s+si\s+applicano\b", "claimed_limit_exemption"),
    # — an instruction to decide now ————————————————————————————————————————
    (r"\bapprove\b[^.]{0,60}\b(?:immediately|now|without)\b", "demands_approval"),
    (
        r"\b(?:sofort|unverz[üu]glich|umgehend)\b[^.]{0,40}\b(?:genehmig\w*|freigeb\w*|"
        r"freizugeben)\b|\b(?:genehmig\w*|freigeb\w*)\b[^.]{0,40}\b(?:sofort|unverz[üu]glich|"
        r"umgehend)\b",
        "demands_approval",
    ),
    (r"\bapprouv\w+\b[^.]{0,60}\b(?:imm[ée]diatement|maintenant|sans)\b", "demands_approval"),
    (r"\bapprov\w+\b[^.]{0,60}\b(?:immediatamente|subito|senza)\b", "demands_approval"),
    # — an instruction to skip verification ————————————————————————————————
    (r"\bwithout\s+further\s+(?:checks|verification|review)\b", "demands_no_checks"),
    (
        r"\bohne\s+(?:weitere|zus[äa]tzliche|erneute)\s+"
        r"(?:pr[üu]fung\w*|kontrolle\w*|[üu]berpr[üu]fung\w*)\b",
        "demands_no_checks",
    ),
    (
        r"\bsans\s+(?:autres?|nouvelle|plus\s+ample)\s+(?:v[ée]rification|contr[ôo]le)\w*\b"
        r"|\bsans\s+(?:v[ée]rification|contr[ôo]le)\s+suppl[ée]mentaire\b",
        "demands_no_checks",
    ),
    (r"\bsenza\s+(?:ulteriori|altri)\s+(?:controlli|verifiche)\b", "demands_no_checks"),
    # — a falsehood about the cardholder, told to suppress the step-up ————————
    (r"\bcardholder\s+is\s+(?:unavailable|not\s+available)\b", "claims_owner_absent"),
    (
        r"\bkarteninhaber(?:in)?\b[^.]{0,30}\b(?:nicht\s+erreichbar|nicht\s+verf[üu]gbar|"
        r"abwesend)\b",
        "claims_owner_absent",
    ),
    (
        r"\btitulaire\b[^.]{0,40}\b(?:indisponible|injoignable|absent)\w*\b",
        "claims_owner_absent",
    ),
    (
        r"\btitolare\b[^.]{0,40}\b(?:non\s+[èe]\s+disponibile|irreperibile|assente)\b",
        "claims_owner_absent",
    ),
    # — an instruction not to ask the human ————————————————————————————————
    (r"\bdo\s+not\s+(?:ask|confirm|verify|check)\b", "demands_no_confirmation"),
    (
        r"\bohne\s+r[üu]ckfrage\b|\bkeine\s+r[üu]ckfrage\b|\bnicht\s+nachfragen\b"
        r"|\bfragen\s+sie\s+nicht\b",
        "demands_no_confirmation",
    ),
    (
        r"\bsans\s+demander\s+(?:de\s+)?confirmation\b"
        r"|\bne\s+(?:pas\s+)?demande[rz]?\s+(?:pas\s+)?(?:de\s+)?confirmation\b",
        "demands_no_confirmation",
    ),
    (
        r"\bnon\s+(?:chiedere|richiedere)\s+(?:alcuna\s+)?conferma\b"
        r"|\bsenza\s+chiedere\s+conferma\b",
        "demands_no_confirmation",
    ),
    # — an instruction to route around a control ————————————————————————————
    (
        r"\b(?:bypass|skip|disregard|override)\b[^.]{0,40}\b(?:check|limit|rule|policy|control)",
        "demands_bypass",
    ),
    # Both word orders: German sends the verb to the end of the clause, so "die Prüfung
    # umgehen" is the ordinary phrasing and the verb-first form is the exception.
    (
        r"\b(?:umgeh\w+|[üu]berspring\w+|au[sß]er\s+kraft)\b[^.]{0,40}\b(?:pr[üu]fung\w*|"
        r"limit\w*|regel\w*|richtlinie\w*|kontrolle\w*)"
        r"|\b(?:pr[üu]fung\w*|limit\w*|regel\w*|richtlinie\w*|kontrolle\w*)\b[^.]{0,40}\b"
        r"(?:umgeh\w+|[üu]berspring\w+|au[sß]er\s+kraft\s+setz\w+)\b",
        "demands_bypass",
    ),
    (
        r"\b(?:contourn\w+|passe[rz]?\s+outre)\b[^.]{0,40}\b(?:contr[ôo]le|limite|r[èe]gle|"
        r"politique)\w*",
        "demands_bypass",
    ),
    (
        r"\b(?:aggirar\w*|salta\w*|eludere)\b[^.]{0,40}\b(?:controll\w+|limit\w+|regol\w+|"
        r"politic\w+)",
        "demands_bypass",
    ),
)
_COMPILED: Final = tuple(
    (re.compile(p, re.IGNORECASE | re.MULTILINE), label) for p, label in MANIPULATION_PATTERNS
)


@dataclass(frozen=True, slots=True)
class Manipulation:
    """A detected attempt to instruct the control layer through merchant text."""

    label: str
    source_field: str
    span: tuple[int, int]
    excerpt: str


@dataclass(frozen=True, slots=True)
class ItemFacts:
    """Typed facts extracted from one item's merchant text. Claims, not truths."""

    return_window_days: int | None = None
    size: int | None = None
    final_sale: bool = False
    return_policy_stated: bool = False
    manipulations: tuple[Manipulation, ...] = field(default=())


def detect_manipulation(text: str, source_field: str) -> tuple[Manipulation, ...]:
    """Find imperative/authority framing. Never alters any limit — evidence only."""
    if not text:
        return ()
    hits: list[Manipulation] = []
    for pattern, label in _COMPILED:
        for m in pattern.finditer(text):
            start, end = max(0, m.start() - 20), min(len(text), m.end() + 40)
            hits.append(
                Manipulation(label, source_field, (m.start(), m.end()), text[start:end].strip())
            )
    return tuple(hits)


_RETURN_DAYS = re.compile(r"return(?:s|ed|able)?[^.;]{0,40}?(\d{1,3})\s*day", re.IGNORECASE)
_SIZE = re.compile(r"\bsize\s+(\d{1,3})\b", re.IGNORECASE)
_FINAL_SALE = re.compile(r"\b(?:final\s+sale|no\s+returns|non-?returnable)\b", re.IGNORECASE)
_POLICY_ABSENT = re.compile(r"return\s+policy\s+not\s+stated", re.IGNORECASE)


def extract_item_facts(item_details: str) -> ItemFacts:
    """Parse product facts out of untrusted text. Returns typed values or None — never a verdict."""
    manipulations = detect_manipulation(item_details or "", "item_details")
    if not item_details:
        return ItemFacts(manipulations=manipulations)

    days = _RETURN_DAYS.search(item_details)
    size = _SIZE.search(item_details)
    final_sale = bool(_FINAL_SALE.search(item_details))
    stated = bool(days) or final_sale
    if _POLICY_ABSENT.search(item_details):
        stated = False

    return ItemFacts(
        return_window_days=int(days.group(1)) if days else None,
        size=int(size.group(1)) if size else None,
        final_sale=final_sale,
        return_policy_stated=stated,
        manipulations=manipulations,
    )


def strip_manipulations(text: str) -> str:
    """Remove detected manipulation spans. Used only by the conformance test in §3."""
    if not text:
        return text
    out = text
    for pattern, _ in _COMPILED:
        out = pattern.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip()


# ---------------------------------------------------------------------------
# Lookalike merchant names
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Fold case, accents and punctuation so confusable names collapse together."""
    folded = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return _NON_ALNUM.sub("", folded.lower())


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance. Small inputs; the simple DP is fast enough."""
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_lookalike(candidate: str, known: str, max_distance: int = 2) -> bool:
    """True when two merchant names are confusably similar but not identical.

    ME0059 'PixelHarbour' vs ME0022 'PixelHarbor' — one letter apart, different merchant_id,
    zero history at the impostor. This is why merchants are joined on merchant_id and never
    on merchant_name.
    """
    a, b = normalize_name(candidate), normalize_name(known)
    if not a or not b or a == b:
        return a == b and candidate != known
    return edit_distance(a, b) <= max_distance


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def safe_display(text: str, max_length: int = 60) -> str:
    """Render untrusted text for a customer-facing message.

    Merchant names and the like must be *shown* — the customer cannot spot a lookalike
    without seeing it — but they must not carry newlines, control characters, or enough
    length to push a real warning off a phone screen.
    """
    cleaned = _CONTROL.sub(" ", text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[: max_length - 1] + "…" if len(cleaned) > max_length else cleaned
