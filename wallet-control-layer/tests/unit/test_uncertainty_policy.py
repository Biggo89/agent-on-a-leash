"""The deterministic compiler reads `uncertainty_policy` from the instruction.

Spec: `specs/policy-ir.md` compiler rule 6. The field decides what an `unknown` verdict
becomes (`decision-rules.md` combination table), so it is the difference between a purchase
the customer is asked about and one that is refused outright.

**What this file is really guarding.** The field used to be a constant. `baseline.py` read

    "uncertainty_policy": "ask" if _UNCERTAIN_ASK.search(instruction) else "ask"

— both branches identical, so the regex was dead and every instruction got `ask` whatever it
said. All five pack instructions end *"Ask me when uncertain"*, so the constant was right for
the pack by luck and wrong for anything else, and the tests that existed could not tell the
difference. These cases can: each one asserts a policy the *instruction* states, and the
asymmetry cases assert the direction the compiler may not move in.
"""

from __future__ import annotations

import pytest

from leash.compile.baseline import compile_instruction

# Every phrasing here states the strict policy outright, in either clause order.
DECLINES = [
    "Buy me a book for CHF 20 or less. If you are not sure, decline.",
    "Buy me a book for CHF 20 or less. If you're not sure, decline.",
    "Buy me a book for CHF 20 or less. If unsure, decline.",
    "Buy me a book for CHF 20 or less. If in doubt, decline.",
    "Buy me a book for CHF 20 or less. When uncertain, decline.",
    "Buy me a book for CHF 20 or less. If you are not sure, then decline.",
    "Buy me a book for CHF 20 or less. Decline if you are not sure.",
    "Buy me a book for CHF 20 or less. Decline when uncertain.",
]

# The default, and the five pack instructions, which all say so explicitly.
ASKS = [
    "Buy me a book for CHF 20 or less. Ask me when uncertain.",
    "Buy me a book for CHF 20 or less.",
    "Order our household groceries for delivery. Keep each order at or below CHF 120 "
    "including delivery, and keep the total across any seven days at or below CHF 300. "
    "Ask me when uncertain.",
]


@pytest.mark.parametrize("instruction", DECLINES)
def test_a_stated_decline_policy_is_enforced(instruction: str) -> None:
    assert compile_instruction(instruction)["uncertainty_policy"] == "decline"


@pytest.mark.parametrize("instruction", ASKS)
def test_ask_is_the_default_and_the_pack_behaviour(instruction: str) -> None:
    assert compile_instruction(instruction)["uncertainty_policy"] == "ask"


@pytest.mark.parametrize(
    "instruction",
    [
        # `decline` about a *thing*, not about uncertainty. The conditional is what carries
        # the meaning, so a bare verb near the word "uncertain" must not be read as a policy.
        "Decline anything over CHF 100 and ask me when uncertain.",
        "Buy me a book. Decline gift wrapping. Ask me when uncertain.",
        # Two sentences: the cue and the verb never share one, so they must not combine.
        "If you are not sure, ask me. Decline anything over CHF 100.",
    ],
)
def test_decline_elsewhere_in_the_sentence_is_not_a_policy(instruction: str) -> None:
    assert compile_instruction(instruction)["uncertainty_policy"] == "ask"


@pytest.mark.parametrize(
    "instruction",
    [
        "Buy me a book for CHF 20 or less. If you are not sure, go ahead.",
        "Buy me a book for CHF 20 or less. If unsure, approve it.",
        "Buy me a book for CHF 20 or less. Use your own judgement when uncertain.",
    ],
)
def test_the_loose_policy_is_never_compiled(instruction: str) -> None:
    """Compiler rule 6: this compiler tightens the field and never loosens it.

    A missed strict cue costs a question. A wrongly-matched loose cue spends the customer's
    money on a guess, so `approve` is not something a regex is allowed to conclude — a model
    may propose it, and a human confirms it.
    """
    assert compile_instruction(instruction)["uncertainty_policy"] != "approve"


def test_the_stated_policy_reaches_the_mandate_draft() -> None:
    """The field is only worth reading if it survives into what the platform is sent."""
    from leash.compile.baseline import to_mandate_payload

    ir = compile_instruction("Buy me a book for CHF 20 or less. If in doubt, decline.")
    assert ir["uncertainty_policy"] == "decline"
    assert to_mandate_payload(ir)["uncertainty_policy"] == "decline"
