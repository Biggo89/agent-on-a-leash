"""Money written in an instruction, and what it is in CHF. Both compilers read it from here.

Every cap is enforced against `billing_amount_chf`, so a limit written in another currency is
converted **once, at compile time**, at the pack's fixed rate (`domain/money.py` `FX_TO_CHF`,
the same table the platform bills with) and **rounded down to the centime**, so the converted
cap is never looser than the words. The rule keeps what was written under `stated`.

Spec: specs/llm-compiler.md §"Foreign-currency caps" · specs/policy-ir.md compiler rule 7
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from ..domain.money import FX_TO_CHF

_NUMBER = r"([0-9]+(?:[.,][0-9]{1,2})?)"
_CODES = "|".join(sorted(FX_TO_CHF))
# `$` is USD: the only dollar the pack has a rate for.
_SYMBOLS = {"€": "EUR", "£": "GBP", "$": "USD"}
_MENTION = re.compile(
    rf"\b({_CODES})\s*{_NUMBER}|([€£$])\s*{_NUMBER}|\b{_NUMBER}\s*({_CODES})\b",
    re.IGNORECASE,
)
# ISO 4217 codes a customer might write that the pack has no rate for. A list rather than any
# three capitals beside a number, because "USB 3 cable" is not a limit in USB, and an open
# question about it on the review screen would teach the customer to ignore open questions.
_NO_RATE = frozenset(
    {
        "AED", "AUD", "BRL", "CAD", "CNY", "CZK", "DKK", "HKD", "HUF", "ILS", "INR", "JPY",
        "KRW", "MXN", "NOK", "NZD", "PLN", "RON", "SEK", "SGD", "THB", "TRY", "ZAR",
    }
)  # fmt: skip
_NO_RATE_CODES = "|".join(sorted(_NO_RATE))
_CODE_BESIDE_NUMBER = re.compile(
    rf"\b({_NO_RATE_CODES})\s*{_NUMBER}|\b{_NUMBER}\s*({_NO_RATE_CODES})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Mention:
    """One amount as the customer wrote it: the number, its currency, and the exact span."""

    amount: Decimal
    currency: str
    text: str
    start: int

    @property
    def foreign(self) -> bool:
        return self.currency != "CHF"

    def chf(self) -> Decimal:
        """The cap in CHF: amount × fixed rate, rounded **down** to the centime."""
        return (self.amount * FX_TO_CHF[self.currency]).quantize(Decimal("0.01"), ROUND_FLOOR)

    def stated(self) -> dict[str, object]:
        """What the rule records about the words, so the customer reads their own limit."""
        return {
            "amount": float(self.amount),
            "currency": self.currency,
            "rate": float(FX_TO_CHF[self.currency]),
        }

    def describe(self) -> str:
        return f"{self.currency} {self.amount:.2f}"

    def times(self, count: int) -> Mention:
        """The same price for `count` units: "CHF 200 per night" for 3 nights is CHF 600.

        Multiplied before conversion, so a foreign total is rounded down once, like the charge
        the platform bills for the whole order (specs/llm-compiler.md §"Derived caps").
        """
        return Mention(self.amount * count, self.currency, self.text, self.start)


def mentions(text: str) -> list[Mention]:
    """Every amount in `text` in a currency the pack can convert, in reading order."""
    found: list[Mention] = []
    for m in _MENTION.finditer(text):
        code_before, n1, symbol, n2, n3, code_after = m.groups()
        currency = (code_before or code_after or _SYMBOLS.get(symbol or "", "")).upper()
        number = n1 or n2 or n3
        found.append(Mention(Decimal(number.replace(",", ".")), currency, m.group(0), m.start()))
    return found


def unconvertible(text: str) -> list[str]:
    """Currency codes beside a number that the pack has no rate for, in reading order."""
    codes: list[str] = []
    for m in _CODE_BESIDE_NUMBER.finditer(text):
        code = (m.group(1) or m.group(4)).upper()
        if code not in codes:
            codes.append(code)
    return codes


def question_for(code: str) -> str:
    return f"Your limit is in {code}, which this wallet cannot convert — what is it in CHF?"
