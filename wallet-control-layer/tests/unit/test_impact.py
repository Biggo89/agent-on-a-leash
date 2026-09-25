"""The counterfactual is a pitch number, so it gets a regression like any other.

`tools/impact.py` answers *what does the understanding layer buy over a spending limit* by
replaying the 45 under four regimes. Every figure comes from the data pack and this engine —
the pack ships no interchange rate, no dispute probability and no cost of a blocked purchase,
so there is nothing here that we made up and nothing that can quietly become wrong.

What this module guards is that the claim on the slide is still the claim the code produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

import impact  # noqa: E402

from leash.adapters.datapack import DataPack, data_dir  # noqa: E402
from leash.adapters.history import HistoryIndex  # noqa: E402
from leash.domain.money import Money  # noqa: E402
from leash.domain.types import Decision  # noqa: E402

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")
AMOUNTS = {a["authorization_id"]: Money.from_value(a["billing_amount_chf"]) for a in PACK.attempts}
BOARDS = {name: impact.run(PACK, HISTORY, checks) for name, (_, checks) in impact.REGIMES.items()}
HELD = {a for a, d in BOARDS["full mandate"].items() if d is not Decision.APPROVE}
ORDINARY = {a for a, d in BOARDS["full mandate"].items() if d is Decision.APPROVE}


def test_the_full_mandate_regime_is_the_shipped_board() -> None:
    """The comparison is worthless if its rightmost column is not what we actually ship."""
    from tests.board import replay_board

    shipped = {a: r.decision for a, r in replay_board().items()}
    assert BOARDS["full mandate"] == shipped


@pytest.mark.parametrize(
    ("regime", "approvals"),
    [("no control", 45), ("per-order cap", 39), ("all limits", 35), ("full mandate", 17)],
)
def test_each_regime_approves_what_the_table_says(regime: str, approvals: int) -> None:
    assert sum(1 for d in BOARDS[regime].values() if d is Decision.APPROVE) == approvals


def test_spending_limits_alone_still_approve_most_of_what_we_hold() -> None:
    """The headline. A limit cannot see anything that is not about the amount."""
    leaked = [a for a in HELD if BOARDS["all limits"][a] is Decision.APPROVE]
    total = Money.zero()
    for auth_id in leaked:
        total = total + AMOUNTS[auth_id]
    assert (len(leaked), len(HELD)) == (19, 28)
    assert str(total) == "3836.28"


def test_the_full_mandate_approves_every_ordinary_purchase() -> None:
    """The other half of the brief: "without blocking ordinary purchases unnecessarily"."""
    assert all(BOARDS["full mandate"][a] is Decision.APPROVE for a in ORDINARY)


def test_spending_limits_alone_also_over_block() -> None:
    """A limit is worse on *both* axes, which is the part a limit's defender never expects.

    AU0007 carries a fragrance gift set on a household-groceries mandate. Limits alone cannot
    see a basket, so that regime approves it, spends the rolling budget on it, and then
    refuses AU0008 — an ordinary grocery order this engine approves.
    """
    blocked = sorted(a for a in ORDINARY if BOARDS["all limits"][a] is not Decision.APPROVE)
    assert blocked == ["AU0008"]
    assert BOARDS["all limits"]["AU0007"] is Decision.APPROVE
    assert BOARDS["full mandate"]["AU0007"] is not Decision.APPROVE


def test_no_regime_invents_a_figure_the_pack_does_not_ship() -> None:
    """Guard on the argument itself, not on the arithmetic.

    The moment this tool grows an interchange rate or a dispute probability it stops being a
    measurement of the organizers' data and becomes a model of our own assumptions — which is
    a weaker claim wearing a stronger costume. The data pack ships none of those figures, so
    any of them appearing here would have been invented.

    Scans code only. The module docstring names these terms precisely in order to say it does
    not use them, and a check that could not tell prose from an expression would forbid the
    explanation along with the thing explained.
    """
    import ast

    source_path = Path(__file__).resolve().parent.parent.parent / "tools" / "impact.py"
    tree = ast.parse(source_path.read_text(), filename=str(source_path))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    forbidden = ("interchange", "dispute", "chargeback", "write_off", "writeoff", "fee_rate")
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value not in docstrings:
                for term in forbidden:
                    if term in node.value.lower():
                        offenders.append(f"line {node.lineno}: string {node.value[:50]!r}")
            # A rate is a float nobody can source. Integers are counts, which are facts.
            elif isinstance(node.value, float):
                offenders.append(f"line {node.lineno}: float literal {node.value!r}")
        elif isinstance(node, ast.Name) and any(t in node.id.lower() for t in forbidden):
            offenders.append(f"line {node.lineno}: name {node.id!r}")

    assert not offenders, "impact.py grew an assumed figure:\n" + "\n".join(offenders)
