"""Familiarity index built from authorization_history.csv.

Familiarity is NOT carried on the live event. "Shops I have used before" and device novelty
have to be computed from the historical file, which the API also serves at
GET /v1/reference-data/authorization-history.csv.

Merchants are keyed on merchant_id and never on merchant_name — ME0022 'PixelHarbor' and
ME0059 'PixelHarbour' exist specifically to punish a name join.

Counted **twice**: once per card, and once per customer. "Shops I have used before" is a
statement about the person, and 17 of the 20 pack customers hold more than one card, so a
card-only count answers a question the cardholder did not ask
(specs/check-merchant-permitted.md §"Card or person"). The history file carries `customer_id`
on every row, so both counters come out of the same pass and no call site has to change.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class HistoryIndex:
    merchant: Counter[tuple[str, str]] = field(default_factory=Counter)  # (card_id, merchant_id)
    device: Counter[tuple[str, str]] = field(default_factory=Counter)  # (card_id, device_id)
    names: dict[str, set[str]] = field(default_factory=dict)  # card_id -> merchant_ids used
    #: (customer_id, merchant_id) -> approvals across **every card the person holds**.
    customer_merchant: Counter[tuple[str, str]] = field(default_factory=Counter)
    #: card_id -> customer_id, read from the history file rather than joined through
    #: cards.csv + accounts.csv: it is the same source as the counts, so the two can never
    #: disagree, and a card with no history simply has no owner here.
    owner: dict[str, str] = field(default_factory=dict)
    #: card_id -> approved rows of any kind. Zero is not "never shopped" but "nothing known":
    #: the live API's cardholders have no row at all (specs/check-merchant-permitted.md
    #: §"No history at all").
    rows: Counter[str] = field(default_factory=Counter)

    @classmethod
    def load(cls, history_csv: Path) -> HistoryIndex:
        idx = cls()
        with history_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["status"] != "approved":  # declines are attempts, not completed spend
                    continue
                card, customer = row["card_id"], row.get("customer_id", "")
                idx.rows[card] += 1

                # Merchant familiarity counts **purchases**, not every approved row. The file
                # also carries refunds and cash withdrawals (`transaction_type`), and the
                # sentence the customer reads is "a shop you have used before (N previous
                # purchases)" — so N has to be purchases or the message is not true.
                #
                # Measured before changing it: excluding refunds moves 10 of the 45 attempts,
                # all at one merchant, from 23 prior purchases to 22. Nothing crosses the
                # minimum, so no decision moves. This is an accuracy fix to a number the
                # customer is shown, not a behaviour change.
                if row.get("transaction_type") == "purchase":
                    idx.merchant[(card, row["merchant_id"])] += 1
                    idx.names.setdefault(card, set()).add(row["merchant_id"])
                    if customer:
                        idx.customer_merchant[(customer, row["merchant_id"])] += 1

                # Device familiarity deliberately counts *all* approved activity. The question
                # it answers is "has this card been used from this device before", not "was a
                # purchase made from it" — a refund or a withdrawal from the cardholder's own
                # phone is still evidence the phone is theirs, and treating it otherwise would
                # invent device novelty where there is none.
                if row["customer_device_id"]:  # empty on in_store/atm rows
                    idx.device[(card, row["customer_device_id"])] += 1
                if customer:
                    idx.owner.setdefault(card, customer)
        return idx

    def has_history(self, card_id: str) -> bool:
        """Whether the file says anything about this card at all."""
        return self.rows[card_id] > 0

    def merchant_approvals(self, card_id: str, merchant_id: str) -> int:
        return self.merchant[(card_id, merchant_id)]

    def customer_merchant_approvals(self, card_id: str, merchant_id: str) -> int:
        """Approvals at this merchant across every card the cardholder holds.

        Zero when the card has no history at all, which is the honest answer: without a row
        naming the owner there is no person to count for, and falling back to the card's own
        figure would make this silently identical to `merchant_approvals`.
        """
        customer = self.owner.get(card_id)
        if customer is None:
            return 0
        return self.customer_merchant[(customer, merchant_id)]

    def device_approvals(self, card_id: str, device_id: str) -> int:
        return self.device[(card_id, device_id)]

    def known_merchants(self, card_id: str) -> set[str]:
        return self.names.get(card_id, set())
