"""The organizers' read-only data pack, loaded once.

Lives in ``adapters/`` because it is an input to the engine, not part of the offline replica:
merchant records feed lookalike detection and the scenario catalogue carries the cardholder
instructions. ``sandbox/`` imports this, never the other way round.

The pack is never written to. Resolution order is $LEASH_DATA_DIR, the sibling
``viseca-2026/data`` checkout, then a local ``data/`` directory.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.settings import register_categories

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    """Resolve the read-only data pack: $LEASH_DATA_DIR, then ../viseca-2026/data, then ./data."""
    candidate = os.environ.get("LEASH_DATA_DIR")
    if candidate and Path(candidate).is_dir():
        return Path(candidate)
    repo = repo_data_dir()
    if repo is not None:
        return repo
    raise FileNotFoundError(
        "Data pack not found. Set LEASH_DATA_DIR to the organizers' viseca-2026/data directory."
    )


def repo_data_dir() -> Path | None:
    """The organizers' repo pack, whatever $LEASH_DATA_DIR says.

    The live pack (`adapters/livepack.py`) is written from the API and carries the repo's
    schemas across, so it needs to find the repo pack even while LEASH_DATA_DIR points at
    itself.
    """
    for candidate in (_PROJECT_ROOT.parent / "viseca-2026" / "data", _PROJECT_ROOT / "data"):
        if candidate.is_dir():
            return candidate
    return None


def _rows(name: str) -> list[dict[str, str]]:
    with (data_dir() / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def merchant_places() -> frozenset[str]:
    """The words the pack's shop cities are written with, lowercase (`merchants.csv`).

    A place is where a shop is, never a word a product is named with, so the compile guard
    drops a keyword that is one of these and no product carries (specs/llm-compiler.md, rail
    5d). Words under three letters ("St." in "St. Gallen") are left out: a place too short to
    tell from an ordinary word proves nothing.
    """
    from ..domain.checks.item import name_tokens

    return frozenset(
        word
        for row in _rows("merchants.csv")
        for word in name_tokens(row["merchant_city"])
        if len(word) > 2
    )


def item_catalogue() -> dict[str, list[str]]:
    """Every catalogue product name, per item category (`items.csv`).

    The data dictionary guarantees a cart line's `item_name` matches its catalogue row, so this
    is the whole set of names the item checks can ever see — which is what lets the compile
    guard say a keyword matches *no* product (specs/llm-compiler.md, rail 5b).
    """
    catalogue: dict[str, list[str]] = {}
    for row in _rows("items.csv"):
        catalogue.setdefault(row["item_category"], []).append(row["item_name"])
    register_categories(catalogue, ())  # the guard reads both; they must agree
    return catalogue


@dataclass(frozen=True, slots=True)
class DataPack:
    merchants: dict[str, dict[str, str]]
    attempts: list[dict[str, str]]
    items: dict[str, list[dict[str, str]]]
    scenarios: dict[str, dict[str, str]]
    authorities: dict[str, dict[str, str]]
    fx: dict[str, float]
    customers: dict[str, dict[str, str]]
    cards: dict[str, dict[str, str]]
    accounts: dict[str, dict[str, str]]

    @classmethod
    def load(cls) -> DataPack:
        items: dict[str, list[dict[str, str]]] = {}
        for row in _rows("purchase_attempt_items.csv"):
            items.setdefault(row["authorization_id"], []).append(row)
        for lines in items.values():
            lines.sort(key=lambda r: int(r["line_no"]))
        attempts = sorted(
            _rows("purchase_attempts.csv"),
            key=lambda r: (r["scenario_id"], int(r["replay_order"])),
        )
        merchants = {r["merchant_id"]: r for r in _rows("merchants.csv")}
        # The live pack adds categories the repo pack never used; the guard, the model's
        # schema and the UI's options must know them (domain/settings.py).
        register_categories(
            (r["item_category"] for r in _rows("items.csv")),
            (r["merchant_category"] for r in merchants.values()),
        )
        return cls(
            merchants=merchants,
            attempts=attempts,
            items=items,
            scenarios={r["scenario_id"]: r for r in _rows("scenario_catalogue.csv")},
            authorities={r["authority_id"]: r for r in _rows("scenario_authorities.csv")},
            fx={r["from_currency"]: float(r["rate"]) for r in _rows("fx_rates.csv")},
            customers={r["customer_id"]: r for r in _rows("customers.csv")},
            cards={r["card_id"]: r for r in _rows("cards.csv")},
            accounts={r["account_id"]: r for r in _rows("accounts.csv")},
        )

    def scenario_attempts(self, scenario_id: str) -> list[dict[str, str]]:
        return [a for a in self.attempts if a["scenario_id"] == scenario_id]

    def event_schema(self) -> dict[str, Any]:
        schema = json.loads(
            (data_dir() / "schemas" / "authorization_event.schema.json").read_text()
        )
        return dict(schema)
