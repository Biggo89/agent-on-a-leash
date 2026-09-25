"""The injection invariant from specs/decision-rules.md §3.

No limit, list, threshold or verdict may be modified by a value sourced from untrusted text.
Proof: evaluate every fixture twice — once as-is, once with the manipulation spans removed —
and require the same decision unless the manipulation is itself the cited reason.
"""

from __future__ import annotations

import copy

import pytest

from leash.adapters.history import HistoryIndex
from leash.adapters.parse import parse_event
from leash.compile.baseline import compile_instruction
from leash.domain.evaluator import evaluate
from leash.domain.ledger import Ledger
from leash.domain.sanitize import strip_manipulations
from sandbox.fixtures import DataPack, build_event, data_dir

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")

_MANDATE_STUB = {
    "mandate_id": "TM_TEST",
    "status": "active",
    "customer_id": "CU0000",
    "card_id": "CA0000",
    "profile_id": "PROFILE_TEST",
}


def _event_for(attempt: dict[str, str]) -> dict:
    ir = compile_instruction(PACK.scenarios[attempt["scenario_id"]]["cardholder_instruction"])
    mandate = {
        **_MANDATE_STUB,
        "instruction": ir["source_instruction"],
        "hard_rules": [
            {
                k: v
                for k, v in r.items()
                if k in ("field", "operator", "value", "currency", "scope", "period_days")
            }
            for r in ir["rules"]
        ],
        "uncertainty_policy": ir["uncertainty_policy"],
    }
    return build_event(
        PACK,
        attempt,
        run_id="RUN_TEST",
        mandate=mandate,
        approved_spend_in_period_chf=0.0,
        recent_authorizations=[],
        deadline_seconds=8,
        request_seq=1,
    )


def _decide(event: dict, facets: list[dict] | None = None) -> tuple[str, tuple[str, ...]]:
    ev = parse_event(
        event, history=HISTORY, ledger=Ledger(), merchants=PACK.merchants, period_windows=(7,)
    )
    record = evaluate(
        ev,
        {
            "hard_rules": event["mandate"]["hard_rules"],
            "uncertainty_policy": event["mandate"]["uncertainty_policy"],
            "intent_facets": facets or [],
        },
    )
    return str(record.decision), record.reason_codes


@pytest.mark.parametrize("attempt", PACK.attempts, ids=lambda a: a["authorization_id"])
def test_untrusted_text_cannot_change_a_decision(attempt: dict[str, str]) -> None:
    event = _event_for(attempt)
    facets = compile_instruction(PACK.scenarios[attempt["scenario_id"]]["cardholder_instruction"])[
        "intent_facets"
    ]
    hostile_decision, hostile_codes = _decide(event, facets)

    clean = copy.deepcopy(event)
    for item in clean["authorization"]["items"]:
        item["item_details"] = strip_manipulations(item["item_details"])
    clean_decision, _ = _decide(clean, facets)

    if "merchant_text_manipulation" in hostile_codes:
        return  # the detector itself is the cited reason — a difference is legitimate
    assert hostile_decision == clean_decision, (
        f"{attempt['authorization_id']}: merchant text changed the decision "
        f"({clean_decision} -> {hostile_decision})"
    )


@pytest.mark.parametrize("attempt", PACK.attempts, ids=lambda a: a["authorization_id"])
def test_engine_never_raises_and_stays_within_budget(attempt: dict[str, str]) -> None:
    ev = parse_event(
        _event_for(attempt),
        history=HISTORY,
        ledger=Ledger(),
        merchants=PACK.merchants,
        period_windows=(7,),
    )
    record = evaluate(ev, {"hard_rules": [], "uncertainty_policy": "ask"})
    assert str(record.decision) in ("approve", "decline", "step_up")
    assert record.latency_ms < 200, f"latency budget blown: {record.latency_ms:.1f}ms"
    assert record.inputs_digest
