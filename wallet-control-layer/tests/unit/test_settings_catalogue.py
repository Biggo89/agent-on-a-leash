"""The editable-settings catalogue, and the one guarantee it exists to make.

specs/customer-settings.md §7: **a setting no check reads is not "unsupported", it is inert.**
It renders on the customer's screen as an enforced rule and enforces nothing — which is worse
than not offering it, because the customer believes they are protected.

So every entry names the check that reads it, and this file is what stops that claim drifting:
rename a check and the catalogue fails here rather than the UI quietly offering a dead control.
"""

from __future__ import annotations

import pytest

from leash.domain.evaluator import CHECKS
from leash.domain.settings import (
    BY_KEY,
    FACET_KINDS,
    FACET_REQUIRE_KEYS,
    SETTINGS,
    STANDING_KEYS,
    TASK_ONLY_KINDS,
    catalogue,
    setting_for_facet,
)

CHECK_IDS = {getattr(c, "__name__", "").removeprefix("check_") for c in CHECKS}


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s.key)
def test_every_setting_names_a_check_that_reads_it(setting) -> None:  # type: ignore[no-untyped-def]
    # Two settings are read by the combination step rather than by a check: what to do with
    # an unestablished fact, and how much evidence it takes to interrupt. Both are properties
    # of how verdicts combine, not of any one verdict — and the list is closed so a third
    # cannot be added without saying why here.
    if setting.check == "evaluator.combine":
        assert setting.key in {"uncertainty_policy", "step_up_threshold"}
        return
    assert setting.check in CHECK_IDS, (
        f"{setting.key} claims to be read by {setting.check!r}, which is not a registered "
        f"check: {sorted(CHECK_IDS)}"
    )


@pytest.mark.parametrize("kind", sorted(FACET_KINDS))
def test_every_facet_kind_is_offerable_or_deliberately_not(kind: str) -> None:
    """A facet the checks read with no catalogue entry is a control nobody can reach."""
    assert setting_for_facet(kind) is not None, f"{kind} is enforced but cannot be edited"


def test_task_only_kinds_are_not_standing() -> None:
    """`item_identity` and `item_attribute` describe the errand, not the customer (§3.2)."""
    for kind in TASK_ONLY_KINDS:
        setting = setting_for_facet(kind)
        assert setting is not None and not setting.standing
        assert setting.key not in STANDING_KEYS


def test_the_period_limit_is_standing_again() -> None:
    """It stopped being standing while one window displaced the others (§3.4).

    Multi-window enrichment made every stated window enforceable, so a customer can carry a
    monthly ceiling across errands again.
    """
    assert BY_KEY["period_limit_chf"].standing


def test_facet_targets_match_the_require_allowlist() -> None:
    """The catalogue and the compile guard read the same table; prove they still agree."""
    for setting in SETTINGS:
        if not setting.target.startswith("facet:"):
            continue
        kind = setting.target.removeprefix("facet:")
        assert kind in FACET_REQUIRE_KEYS, f"{setting.key} targets an unknown facet kind"


def test_the_published_catalogue_is_json_shaped() -> None:
    """`GET /v1/config` serves this, and leash-demo's check.mjs asserts its table against it."""
    rows = catalogue()
    assert {r["key"] for r in rows} == set(BY_KEY)
    for row in rows:
        assert set(row) >= {"key", "label", "control", "target", "check", "tighter", "standing"}
        assert isinstance(row["standing"], bool)
