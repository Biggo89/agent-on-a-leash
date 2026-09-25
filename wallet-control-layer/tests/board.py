"""Replay all 45 fixtures in process, through the same path as `make replay`.

Two modules need the whole board rather than one event: `test_customer_message.py`, which
pins the demo sentences, and `test_no_hardcoding.py`, which replays it twice under different
identifiers. Keeping one replay here means those two can never disagree about what the board
is — and `test_the_board_this_module_pins_is_the_board_that_ships` still checks this harness
against `out/baseline.json`, so a drift from `make replay` fails loudly either way.

A shared ledger per scenario is what makes the window-dependent fixtures land where the board
says; evaluating them in isolation would not.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from leash.adapters.history import HistoryIndex
from leash.adapters.parse import parse_event
from leash.compile.baseline import compile_instruction
from leash.domain.evaluator import evaluate
from leash.domain.ledger import Ledger
from leash.domain.policy import period_windows
from leash.domain.types import Decision, DecisionRecord, EnrichedEvent
from sandbox.fixtures import DataPack, build_event, data_dir

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")

#: Rewrites one built event before it is parsed. Used only to prove that nothing in the
#: engine reads an identifier — see `test_no_hardcoding.py`.
Rewrite = Callable[[dict[str, Any]], dict[str, Any]]


def replay_board(rewrite: Rewrite | None = None) -> dict[str, DecisionRecord]:
    """Every fixture, in replay order, keyed on its **public** authorization id.

    The key comes from the data pack rather than from the event, so a run whose identifiers
    were rewritten is still comparable, row for row, with one that was not.
    """
    records: dict[str, DecisionRecord] = {}
    for scenario_id in sorted(PACK.scenarios):
        ir = compile_instruction(PACK.scenarios[scenario_id]["cardholder_instruction"])
        windows = period_windows(ir["rules"])
        mandate = {
            "mandate_id": "TM_TEST",
            "status": "active",
            "customer_id": "CU0000",
            "card_id": "CA0000",
            "profile_id": "PROFILE_TEST",
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
        ledger = Ledger()
        seen: list[EnrichedEvent] = []
        attempts = sorted(
            (a for a in PACK.attempts if a["scenario_id"] == scenario_id),
            key=lambda a: int(a["replay_order"]),
        )
        for seq, attempt in enumerate(attempts, start=1):
            event = build_event(
                PACK,
                attempt,
                run_id="RUN_TEST",
                mandate=mandate,
                approved_spend_in_period_chf=None,
                recent_authorizations=[],
                deadline_seconds=8,
                request_seq=seq,
            )
            if rewrite is not None:
                event = rewrite(event)
            ev = parse_event(
                event,
                history=HISTORY,
                ledger=ledger,
                merchants=PACK.merchants,
                period_windows=windows,
                seen_in_run=seen,
            )
            record = evaluate(
                ev,
                {
                    "hard_rules": mandate["hard_rules"],
                    "uncertainty_policy": mandate["uncertainty_policy"],
                    "intent_facets": ir["intent_facets"],
                },
            )
            if record.decision is Decision.APPROVE:
                ledger.record_approval(ev.authorization_id, ev.timestamp, ev.billing_amount_chf)
            elif record.decision is Decision.STEP_UP:
                ledger.record_step_up(ev.authorization_id, ev.timestamp, ev.billing_amount_chf)
            seen.append(ev)
            records[attempt["authorization_id"]] = record
    return records
