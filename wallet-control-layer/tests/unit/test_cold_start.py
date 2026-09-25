"""A card with no history at all: the run's own approvals are the history.

specs/check-merchant-permitted.md §"No history at all" and specs/check-session-integrity.md
§"A card with no history". The live API's cardholders have no row in the history file, and
reading that silence as "never bought there, never seen this phone" declined or stepped up
every live order on 2026-09-24. These tests drive `parse_event` end to end: a raw event, the
real history index, a ledger that sees an approval and a resolved step-up.
"""

from __future__ import annotations

import copy
from typing import Any

from leash.adapters.history import HistoryIndex
from leash.adapters.parse import parse_event
from leash.domain.evaluator import evaluate
from leash.domain.ledger import Ledger
from leash.domain.types import EnrichedEvent
from sandbox.fixtures import DataPack, build_event, data_dir

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")
NEW_CARD = "CA9999"  # in no row of the history file, like the live API's CA1331
FAMILIAR = [{"kind": "merchant_familiarity", "require": {"prior_approvals_min": 1}}]
_MANDATE = {
    "mandate_id": "TM_TEST",
    "status": "active",
    "customer_id": "CU9999",
    "card_id": NEW_CARD,
    "instruction": "Buy the camera lens I chose, from a seller I have bought from before.",
    "hard_rules": [],
    "uncertainty_policy": "ask",
    "profile_id": "PROFILE_TEST",
}
_TEMPLATE = build_event(
    PACK,
    next(a for a in PACK.attempts if a["authorization_id"] == "AU0035"),
    run_id="RUN_TEST",
    mandate=_MANDATE,
    approved_spend_in_period_chf=0.0,
    recent_authorizations=[],
    deadline_seconds=8,
    request_seq=1,
)


def _raw(
    auth_id: str, merchant_id: str, merchant_name: str, device: str, card: str = NEW_CARD
) -> dict[str, Any]:
    event = copy.deepcopy(_TEMPLATE)
    a = event["authorization"]
    a["authorization_id"] = auth_id
    a["card_id"] = card
    a["customer_device_id"] = device
    a["merchant"]["merchant_id"] = merchant_id
    a["merchant"]["merchant_name"] = merchant_name
    a["recent_attempt_count_10m"] = 0
    return event


def _parse(event: dict[str, Any], ledger: Ledger, seen: list[EnrichedEvent]) -> EnrichedEvent:
    return parse_event(
        event,
        history=HISTORY,
        ledger=ledger,
        merchants=PACK.merchants,
        period_windows=(7,),
        seen_in_run=seen,
    )


def _check(ev: EnrichedEvent, check_id: str) -> list[str]:
    record = evaluate(
        ev, {"hard_rules": [], "uncertainty_policy": "ask", "intent_facets": FAMILIAR}
    )
    return [
        str(r.reason_code)
        for r in record.check_results
        if r.check_id.startswith(check_id) and r.reason_code
    ]


def test_the_new_card_really_has_no_history() -> None:
    assert not HISTORY.has_history(NEW_CARD)
    assert HISTORY.has_history("CA0039")


def test_first_order_asks_about_the_shop_and_not_about_the_phone() -> None:
    ev = _parse(_raw("AU_A1", "ME0046", "LensTrail", "DVC-A"), Ledger(), [])

    assert ev.enrichment.familiarity_basis == "run"
    assert ev.enrichment.run_approvals == 0
    assert _check(ev, "merchant_permitted") == ["merchant_history_unavailable"]
    assert "device_novel" not in _check(ev, "session")
    record = evaluate(
        ev, {"hard_rules": [], "uncertainty_policy": "ask", "intent_facets": FAMILIAR}
    )
    assert str(record.decision) == "step_up"


def test_a_step_up_the_customer_approved_counts_for_the_next_order() -> None:
    ledger, seen = Ledger(), []
    first = _parse(_raw("AU_A1", "ME0046", "LensTrail", "DVC-A"), ledger, seen)
    ledger.record_step_up(first.authorization_id, first.timestamp, first.billing_amount_chf)
    seen.append(first)
    ledger.resolve(first.authorization_id, approved=True)  # the customer said yes on the phone

    again = _parse(_raw("AU_A2", "ME0046", "LensTrail", "DVC-A"), ledger, seen)
    assert again.enrichment.merchant_prior_approvals == 1
    assert again.enrichment.device_prior_approvals == 1
    assert _check(again, "merchant_permitted") == ["merchant_familiar"]
    assert "device_novel" not in _check(again, "session")

    elsewhere = _parse(_raw("AU_A3", "ME0284", "Silver Works", "DVC-A"), ledger, seen)
    assert _check(elsewhere, "merchant_permitted") == ["merchant_history_unavailable"]

    other_phone = _parse(_raw("AU_A4", "ME0046", "LensTrail", "DVC-B"), ledger, seen)
    assert "device_novel" in _check(other_phone, "session")


def test_a_declined_or_unanswered_step_up_teaches_nothing() -> None:
    ledger, seen = Ledger(), []
    first = _parse(_raw("AU_A1", "ME0046", "LensTrail", "DVC-A"), ledger, seen)
    ledger.record_step_up(first.authorization_id, first.timestamp, first.billing_amount_chf)
    seen.append(first)

    pending = _parse(_raw("AU_A2", "ME0046", "LensTrail", "DVC-B"), ledger, seen)
    assert pending.enrichment.merchant_prior_approvals == 0
    assert pending.enrichment.run_approvals == 0
    assert "device_novel" not in _check(pending, "session")

    ledger.resolve(first.authorization_id, approved=False)
    declined = _parse(_raw("AU_A3", "ME0046", "LensTrail", "DVC-B"), ledger, seen)
    assert _check(declined, "merchant_permitted") == ["merchant_history_unavailable"]


def test_a_shop_approved_in_this_run_is_protected_from_lookalikes() -> None:
    ledger, seen = Ledger(), []
    first = _parse(_raw("AU_A1", "ME0022", "PixelHarbor", "DVC-A"), ledger, seen)
    ledger.record_approval(first.authorization_id, first.timestamp, first.billing_amount_chf)
    seen.append(first)

    impostor = _parse(_raw("AU_A2", "ME0059", "PixelHarbour", "DVC-A"), ledger, seen)
    assert impostor.enrichment.merchant_lookalike_of == "ME0022"


def test_a_card_with_history_is_judged_exactly_as_before() -> None:
    ev = _parse(_raw("AU_H1", "ME0026", "RainThread", "DVC-UNSEEN", card="CA0023"), Ledger(), [])

    assert ev.enrichment.familiarity_basis == "history"
    assert _check(ev, "merchant_permitted") == ["merchant_not_permitted"]
    assert "device_novel" in _check(ev, "session")
