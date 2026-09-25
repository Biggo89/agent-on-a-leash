"""Where a rule came from — typed, because a mandate now has more than one source.

Until layering, every rule and facet carried ``provenance: str``: the verbatim span of the
instruction it was compiled from, which the demo highlights inside the customer's own
sentence. That is the best mechanism in the build and it does not survive composition as-is —
a rule contributed by the standing preferences layer has no span in the instruction, and a
facet merged from two layers has two origins.

So provenance becomes a list of ``{source, quote}``:

    instruction   the cardholder's own words, compiled      → the existing case
    preferences   a standing setting they saved
    profile       seeded from the customer profile and accepted by them
    amendment     edited in the app after the mandate was confirmed
    account       a platform limit on the account, not editable

A bare string still reads as ``instruction``, so the five pack mandates and every existing
fixture render exactly as before. specs/customer-settings.md §5.

Pure. No I/O, no clock.
"""

from __future__ import annotations

from typing import Any

SOURCES: tuple[str, ...] = ("instruction", "preferences", "profile", "amendment", "account")

#: What a customer message appends when it names a limit. The instruction is the default and
#: says nothing — "your CHF 200 per-order limit" already refers to words they wrote, and
#: adding an attribution to all 45 existing messages would be noise. Every other source *must*
#: be named, or the customer reads their instruction, sees a different number, and concludes
#: the engine is wrong. specs/customer-message.md §4.
ATTRIBUTION: dict[str, str] = {
    "instruction": "",
    "preferences": " you set in your preferences",
    "profile": " from your profile",
    "amendment": " you set in the app",
    "account": " on your account",
}


def normalise(value: Any) -> tuple[dict[str, str], ...]:
    """Any accepted provenance shape → the canonical tuple of ``{source, quote}``.

    Total: an unrecognised shape yields an empty provenance rather than raising, because this
    runs inside the decision path where nothing may raise (AGENTS.md §3.5).
    """
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return ({"source": "instruction", "quote": value},)
    if isinstance(value, dict):
        return (_entry(value),) if value else ()
    if isinstance(value, (list, tuple)):
        out: list[dict[str, str]] = []
        for item in value:
            out.extend(normalise(item))
        return tuple(out)
    return ()


def _entry(raw: dict[str, Any]) -> dict[str, str]:
    source = str(raw.get("source") or "instruction")
    return {
        "source": source if source in SOURCES else "instruction",
        "quote": str(raw.get("quote") or ""),
    }


def source_of(carrier: dict[str, Any] | None) -> str:
    """The layer a rule or facet came from. Defaults to ``instruction``.

    First entry wins: composition appends, and the first contributor is the one whose wording
    the customer message should use.
    """
    if not carrier:
        return "instruction"
    entries = normalise(carrier.get("provenance"))
    return entries[0]["source"] if entries else "instruction"


def attribution(carrier: dict[str, Any] | None) -> str:
    """The clause a customer message appends after naming a limit. Empty for the instruction."""
    return ATTRIBUTION.get(source_of(carrier), "")


def quotes(carrier: dict[str, Any] | None, source: str = "instruction") -> list[str]:
    """Spans to highlight, for one source. The demo highlights ``instruction`` in the sentence."""
    if not carrier:
        return []
    return [
        e["quote"]
        for e in normalise(carrier.get("provenance"))
        if e["source"] == source and e["quote"]
    ]


def stamp(carrier: dict[str, Any], source: str, quote: str = "") -> dict[str, Any]:
    """Return a copy of a rule or facet whose provenance names ``source``.

    Used when a layer contributes a rule the customer never wrote in an instruction — a
    standing preference, an account ceiling, an edit made in the app.
    """
    return {**carrier, "provenance": [{"source": source, "quote": quote}]}


def merge(*carriers: dict[str, Any] | None) -> list[dict[str, str]]:
    """Provenance for a facet merged from several layers — every origin, in layer order.

    Deduplicated: two layers stating the same thing with the same words is one origin, and a
    review screen listing it twice invites the customer to wonder which one is real.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for carrier in carriers:
        if not carrier:
            continue
        for entry in normalise(carrier.get("provenance")):
            key = (entry["source"], entry["quote"])
            if key not in seen:
                seen.add(key)
                out.append(entry)
    return out
