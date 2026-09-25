"""Board-wide properties of the customer message, and the demo beats pinned verbatim.

specs/customer-message.md. The vectors in tests/vectors/customer_message.yaml pin the
*composition* rules on synthetic results; this module runs the real 45-fixture board and
asserts the two properties that only make sense over the whole of it — plus the exact text of
the messages the demo script (GUIDELINES.md §11) puts on screen, because Phase 5 work that is
not pinned is Phase 6 work that silently regresses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leash.domain.types import Decision
from tests.board import replay_board

ROOT = Path(__file__).resolve().parent.parent.parent

BOARD = replay_board()
IDS = sorted(BOARD)


# --------------------------------------------------------------------- board invariants


@pytest.mark.parametrize("auth_id", IDS)
def test_message_never_leaks_a_reason_code(auth_id: str) -> None:
    """decision-rules.md §8: no internal rule IDs. The customer reads prose, not vocabulary."""
    record = BOARD[auth_id]
    for code in record.reason_codes:
        assert code not in record.customer_message, f"{auth_id} leaked {code!r}"


@pytest.mark.parametrize("auth_id", IDS)
def test_message_never_quotes_untrusted_text_back(auth_id: str) -> None:
    """Forwarding an injection to the human it is aimed at would be the attack succeeding.

    The excerpt stays in `evidence`, behind `safe_display`, for the audit record only.
    """
    record = BOARD[auth_id]
    excerpts = [e.value for e in record.evidence if e.field == "manipulation_excerpt" and e.value]
    for excerpt in excerpts:
        assert excerpt not in record.customer_message, f"{auth_id} quoted the injection back"
        # Not even a fragment: the shortest hostile phrase is well under this.
        assert excerpt[:25] not in record.customer_message


@pytest.mark.parametrize("auth_id", IDS)
def test_message_names_only_cited_reasons(auth_id: str) -> None:
    """specs/customer-message.md §2 — the cause is a subset of the record's reason codes.

    Checked through the details themselves: a detail belonging to a result that was not cited
    must not appear in the cause. It may still appear inside the "also noticed" sentence, so
    the assertion is scoped to the text before it.
    """
    record = BOARD[auth_id]
    cause = record.customer_message.split("Also noticed,")[0]
    for result in record.check_results:
        if result.detail and result.reason_code not in record.reason_codes:
            assert result.detail not in cause, (
                f"{auth_id}: message argues {result.reason_code!r}, which is not on the record"
            )


@pytest.mark.parametrize("auth_id", IDS)
def test_every_message_is_well_formed(auth_id: str) -> None:
    message = BOARD[auth_id].customer_message
    assert message.startswith(("Approved: ", "Declined: ", "Needs your confirmation: "))
    assert message.endswith(".")
    assert "  " not in message, "double space — a part was joined with an empty one"


def test_the_board_this_module_pins_is_the_board_that_ships() -> None:
    """Guard on the harness itself.

    These messages are only worth pinning if this in-process replay produces the decisions
    `make replay` produces. If the two ever drift, the demo assertions below are pinning a
    board nobody will see.
    """
    baseline = json.loads((ROOT / "out" / "baseline.json").read_text())
    for row in baseline:
        auth_id = str(row["source_authorization_id"])
        assert str(BOARD[auth_id].decision) == row["decision"], (
            f"{auth_id}: in-process replay says {BOARD[auth_id].decision}, "
            f"the saved board says {row['decision']}"
        )


def test_step_ups_always_say_how_to_answer() -> None:
    from leash.domain.evaluator import ANSWER_IN_APP

    step_ups = [r for r in BOARD.values() if r.decision is Decision.STEP_UP]
    assert step_ups, "the board has no step-ups — the demo's fifth beat has nothing to show"
    for record in step_ups:
        assert ANSWER_IN_APP in record.customer_message


def test_approvals_never_carry_an_also_noticed_sentence() -> None:
    """Beat 2: ordinary shopping is not interrupted, including by the wording."""
    for auth_id, record in BOARD.items():
        if record.decision is Decision.APPROVE:
            assert "Also noticed" not in record.customer_message, auth_id


# -------------------------------------------------------------------- the demo beats

DEMO_MESSAGES = {
    # Beat 2 — ordinary purchase, minimal friction, with the 7-day total shown.
    "AU0002": (
        "Approved: CHF 44.50 at Alpine Basket. Within your CHF 120.00 per-order limit; "
        "CHF 44.50 of CHF 300.00 across 7 days; this order contains "
        '"Fresh produce selection", "Breakfast supplies", which is what you asked for.'
    ),
    # Beat 3 — the rolling window. 223.50/300, not the 388 a cumulative counter would show.
    "AU0011": (
        "Approved: CHF 88.00 at Alpine Basket. Within your CHF 120.00 per-order limit; "
        "CHF 223.50 of CHF 300.00 across 7 days; this order contains "
        '"Weekly grocery basket", "Fresh produce selection", which is what you asked for.'
    ),
    # Beat 3b — the split order. Each half clears the CHF 120 cap; together they do not.
    # specs/check-split-order.md.
    "AU0006": (
        "Needs your confirmation: CHF 65.00 at Alpine Basket. 2 orders at Alpine Basket "
        "within a few minutes come to CHF 135.00, against the CHF 120.00 per-order limit. "
        "Each order is within your limit on its own. Approve or decline in the app."
    ),
    # …and what holding AU0006 buys: AU0008 lands on exactly the 7-day ceiling and is
    # approved, where an inflated ledger used to decline an ordinary grocery order.
    "AU0008": (
        "Approved: CHF 65.50 at Alpine Basket. Within your CHF 120.00 per-order limit; "
        "CHF 300.00 of CHF 300.00 across 7 days; this order contains "
        '"Weekly grocery basket", "Fresh produce selection", which is what you asked for.'
    ),
    # Beat 4a — the injection that failed on the facts, and is still reported.
    "AU0037": (
        "Declined: CHF 520.00 at PixelHarbor. CHF 520.00 exceeds the CHF 400.00 per-order "
        "limit. An order of CHF 400.00 or less would be within your limit. Also noticed, "
        "and it did not change this decision: the seller's product description contains "
        "instructions aimed at automated payment systems."
    ),
    # Beat 4b — the lookalike, caught on id-and-history and named in the message.
    "AU0039": (
        "Declined: CHF 340.00 at PixelHarbour. You have not bought from PixelHarbour "
        'before. Also noticed, and it did not change this decision: the seller "PixelHarbour" '
        'has a name that closely resembles "PixelHarbor", a shop you have used.'
    ),
    # Beat 5 — the human keeps control. The only fixture where hostile text alone escalates.
    "AU0040": (
        "Needs your confirmation: CHF 299.00 at PixelHarbor. The seller's product "
        "description contains instructions aimed at automated payment systems. Nothing in "
        "the seller's text changed how this purchase was assessed. Approve or decline in "
        "the app."
    ),
    # "Doesn't it just block everything?" — 450 USD at a US seller, fully compliant.
    "AU0038": (
        "Approved: CHF 391.50 (USD 450.00) at HarborByte. Within your CHF 400.00 per-order "
        "limit; HarborByte is a shop you have used before (21 previous purchases); this "
        'order contains "27-inch computer monitor", which is what you asked for; you have '
        "already bought the 27-inch monitor on this errand; this is order 2."
    ),
    # …an unfamiliar merchant that meets every stated requirement.
    "AU0023": (
        "Approved: CHF 179.00 at Summit Thread. Within your CHF 200.00 per-order limit; "
        "Summit Thread is the kind of shop you asked for (sporting goods); this order can "
        "be returned within 30 days, meeting your 14-day minimum; this order contains "
        '"Road-running shoes", which is what you asked for; this order is in size 43, as '
        "you asked; you have already bought the road-running shoes on this errand; this is "
        "order 3."
    ),
    # …a shop the person has used and this card has not: asked, not refused.
    # specs/check-merchant-permitted.md §"Card or person".
    "AU0044": (
        "Needs your confirmation: CHF 310.00 at Circuit and Pine. You have bought from "
        "Circuit and Pine before, but on a different card and not on this one. Your "
        "instruction names shops you have used, and this card has not been there. "
        "Approve or decline in the app."
    ),
    # …and a retry at a new price after a decline, which is not a duplicate.
    "AU0042": (
        "Approved: CHF 350.00 at PixelHarbor. Within your CHF 400.00 per-order limit; "
        "PixelHarbor is a shop you have used before (6 previous purchases); this order "
        'contains "27-inch computer monitor", which is what you asked for; you have already '
        "bought the 27-inch monitor on this errand; this is order 3; this is a new price for "
        "an order that was previously declined, not a repeat."
    ),
}


@pytest.mark.parametrize("auth_id", sorted(DEMO_MESSAGES), ids=sorted(DEMO_MESSAGES))
def test_demo_message_is_exactly_what_was_rehearsed(auth_id: str) -> None:
    assert BOARD[auth_id].customer_message == DEMO_MESSAGES[auth_id]
