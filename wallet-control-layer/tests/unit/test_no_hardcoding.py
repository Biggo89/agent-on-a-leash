"""Nothing in the engine reads an identifier. Proved, not asserted.

    "Never hardcode to `scenario_id`, authorization IDs, or replay position — prohibited by
     the challenge."  — CLAUDE.md, and `specs/decision-rules.md` §187

That rule has been a claim in three documents and a matter of authorial discipline in the
code. This module makes it a test: replay all 45 fixtures with **every identifier replaced**
by a consistent bijection, and require the same 45 decisions, reason codes and customer
messages.

What is rewritten, and what is deliberately not:

  rewritten   authorization_id · source_authorization_id · related_authorization_id ·
              scenario_id · request_id · mandate_id · profile_id — names the engine may
              carry into a record and must never branch on.

  untouched   every fact. Amounts, timestamps, merchant, cart, device, card_id and the
              delivery order all stay exactly as they were, because they are what a decision
              is legitimately made of. A test that scrambled those would prove nothing except
              that different inputs produce different answers.

`related_authorization_id` is why this is a bijection rather than a fresh id per event: it
links AU0042 to the declined AU0037, and a rewrite that broke that link would turn a
legitimate re-quote into something else and fail for the wrong reason.

Two fixed seeds, so a failure is reproducible.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from tests.board import PACK, replay_board

SEEDS = (20260922, 7)

#: Every place an identifier appears in a built event, as (path-to-dict, key).
_AUTHORIZATION_KEYS = (
    "authorization_id",
    "source_authorization_id",
    "related_authorization_id",
)


def _scrambler(seed: int) -> dict[str, str]:
    """A bijection over the public authorization ids, stable for the whole run."""
    rng = random.Random(seed)
    public = sorted({a["authorization_id"] for a in PACK.attempts})
    shuffled = public[:]
    rng.shuffle(shuffled)
    # Prefixed so a leaked identifier is obvious in a failure message, and so the mapping
    # cannot accidentally be the identity.
    return {original: f"ZZ{new[2:]}" for original, new in zip(public, shuffled, strict=True)}


def _rewrite(mapping: dict[str, str], scenario_names: dict[str, str]) -> Any:
    """Rename every identifier in a built event. Facts are not touched."""

    def rename_authorization(value: str | None) -> str | None:
        if value is None:
            return None
        # Ids are run-scoped (`AU0005-RUN_TEST`); rename the public part, keep the suffix.
        for original, replacement in mapping.items():
            if value.startswith(original):
                return replacement + value[len(original) :]
        return value

    def apply(event: dict[str, Any]) -> dict[str, Any]:
        out = {
            **event,
            "authorization": {**event["authorization"]},
            "mandate": {**event["mandate"]},
        }
        auth = out["authorization"]
        for key in _AUTHORIZATION_KEYS:
            if key in auth:
                auth[key] = rename_authorization(auth[key])
        auth["scenario_id"] = scenario_names[auth["scenario_id"]]
        auth["mandate_id"] = "ZM_SCRAMBLED"
        auth["profile_id"] = "ZP_SCRAMBLED"
        out["mandate"]["mandate_id"] = "ZM_SCRAMBLED"
        out["mandate"]["profile_id"] = "ZP_SCRAMBLED"
        out["request_id"] = "zreq_scrambled_" + str(auth["replay_order"])
        return out

    return apply


def _outcome(record: Any) -> dict[str, Any]:
    return {
        "decision": str(record.decision),
        "reason_codes": list(record.reason_codes),
        "customer_message": record.customer_message,
    }


PUBLISHED = {auth_id: _outcome(r) for auth_id, r in replay_board().items()}


@pytest.mark.parametrize("seed", SEEDS)
def test_renaming_every_identifier_changes_no_decision(seed: int) -> None:
    mapping = _scrambler(seed)
    # Scenario ids keep their documented shape, because the event schema validates it.
    scenario_names = {
        scenario_id: f"SCEN9{index:03d}" for index, scenario_id in enumerate(sorted(PACK.scenarios))
    }
    scrambled = {
        auth_id: _outcome(r)
        for auth_id, r in replay_board(_rewrite(mapping, scenario_names)).items()
    }

    assert sorted(scrambled) == sorted(PUBLISHED)
    differing = {
        auth_id: {"published": PUBLISHED[auth_id], "renamed": scrambled[auth_id]}
        for auth_id in PUBLISHED
        if PUBLISHED[auth_id] != scrambled[auth_id]
    }
    assert not differing, f"the engine read an identifier on: {sorted(differing)}"


def test_the_scramble_actually_scrambles() -> None:
    """A mapping that quietly became the identity would make the test above vacuous."""
    mapping = _scrambler(SEEDS[0])
    assert len(mapping) == 45
    assert all(original != replacement for original, replacement in mapping.items())
    assert len(set(mapping.values())) == 45, "the mapping is not a bijection"


def test_no_source_file_mentions_a_fixture_identifier_outside_a_comment() -> None:
    """The stronger, cheaper form of the same rule: an id in `src/` is either documentation
    or a bug, and there is no third kind.

    Comments and docstrings name fixtures constantly — that is how the specs and the code
    explain themselves. What may never appear is an identifier in an expression.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.parent / "src"
    tokens = ("AU00", "SCEN00", "ME00", "CA00", "IT00", "CU00")
    offenders: list[str] = []
    documented = 0

    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        # Exclude docstrings by identity rather than by length: a *short* docstring naming a
        # fixture is ordinary prose, and a length cutoff would fail on the next one written.
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if not any(token in text for token in tokens):
                continue
            if text in docstrings:
                documented += 1
                continue
            offenders.append(f"{path.relative_to(src)}:{node.lineno} {text[:60]!r}")

    assert not offenders, "fixture identifiers used as values in src/:\n" + "\n".join(offenders)
    # Guard on the guard: the engine's docstrings cite fixtures constantly, so a scan finding
    # nothing at all would mean the token list or the walk had stopped working.
    assert documented > 10, f"only {documented} documented mentions found — is the scan working?"
