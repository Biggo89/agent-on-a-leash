"""Money as integer centimes. No binary floats anywhere in the decision path.

SCEN0001 puts a rolling total on CHF 299.50 against a CHF 300.00 cap. Float arithmetic
changes that outcome, so every amount is an integer count of centimes and every conversion
rounds half-even, exactly as the data dictionary specifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

CENTIMES: Final = Decimal(100)

# Fixed synthetic table, rate_date 2026-08-01. billing_amount_chf = amount * rate.
FX_TO_CHF: Final[dict[str, Decimal]] = {
    "CHF": Decimal("1.000000"),
    "EUR": Decimal("0.950000"),
    "GBP": Decimal("1.120000"),
    "USD": Decimal("0.870000"),
}
SUPPORTED_CURRENCIES: Final = frozenset(FX_TO_CHF)


class CurrencyError(ValueError):
    """An unsupported currency reached the engine."""


@dataclass(frozen=True, order=True, slots=True)
class Money:
    """A CHF amount, stored as an integer number of centimes."""

    centimes: int

    @classmethod
    def from_value(cls, value: object) -> Money:
        """Parse a JSON number/string into centimes via Decimal — never via float."""
        return cls(int((Decimal(str(value)) * CENTIMES).quantize(Decimal(1), ROUND_HALF_EVEN)))

    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    def __add__(self, other: Money) -> Money:
        return Money(self.centimes + other.centimes)

    def __sub__(self, other: Money) -> Money:
        return Money(self.centimes - other.centimes)

    def __mul__(self, factor: int) -> Money:
        return Money(self.centimes * factor)

    @property
    def decimal(self) -> Decimal:
        return (Decimal(self.centimes) / CENTIMES).quantize(Decimal("0.01"), ROUND_HALF_EVEN)

    def __str__(self) -> str:
        return f"{self.decimal:.2f}"

    def __repr__(self) -> str:
        return f"Money({self})"


def to_chf(amount: object, currency: str) -> Money:
    """Convert a row-currency amount to CHF using the fixed table.

    Only for values that carry their own currency (e.g. item unit_price). When the event
    already supplies ``billing_amount_chf``, prefer that field — it is authoritative.
    """
    if currency not in FX_TO_CHF:
        raise CurrencyError(f"unsupported currency: {currency!r}")
    converted = Decimal(str(amount)) * FX_TO_CHF[currency] * CENTIMES
    return Money(int(converted.quantize(Decimal(1), ROUND_HALF_EVEN)))


def sum_money(values: object) -> Money:
    total = Money.zero()
    for v in values:  # type: ignore[attr-defined]
        total = total + v
    return total
