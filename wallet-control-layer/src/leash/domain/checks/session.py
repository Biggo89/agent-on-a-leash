"""Session integrity: does it look like someone other than the cardholder is driving?

    "Pause anything that looks like someone other than me is driving the session."

The verb is *pause*, not decline — the customer is asking for a step-up, and these signals
deliver exactly that.

Emits each signal as its own concern rather than one composite score, so every weight lives in
`evaluator.CONCERN_WEIGHTS` where it can be tuned and shown, and the audit record names which
signal actually fired instead of a single opaque total.

Two properties matter as much as the signals themselves:

  * **No sticky state.** Only the current event's signals are read, so once a run returns to a
    known device the score falls to zero on its own. Sticky "session is compromised" state
    would let one anomalous night poison every later purchase, which is over-blocking in its
    purest form — and SCEN0003 tests for recovery explicitly.
  * **Cross-border is not a signal.** CA0023 has 23 approved Italian transactions; the persona
    takes regular trips to Italy. Scoring foreignness would penalise legitimate purchases and
    reinforce the wrong answer on AU0038.

Spec: specs/check-session-integrity.md
"""

from __future__ import annotations

from typing import Any

from ..types import CheckResult, EnrichedEvent, Evidence, Verdict

VELOCITY_ATTEMPTS = 2


def check_session_integrity(ev: EnrichedEvent, policy: dict[str, Any]) -> tuple[CheckResult, ...]:
    """Report each session signal present on this event; the evaluator weighs them together.

    Always runs. Session integrity is an issuer-level duty rather than a preference a customer
    has to remember to state — the same reasoning as `merchant_lookalike`. Verified safe
    against the pack: only AU0026-AU0030 carry any session signal at all.
    """
    signals: list[CheckResult] = []

    # A card with no history at all has only this run to compare against. Until the run has
    # approved something there is no baseline, so the first device is the baseline rather than
    # a novelty; after that, a device none of the approved orders used is new to this session.
    # specs/check-session-integrity.md §"A card with no history".
    run_basis = ev.enrichment.familiarity_basis == "run"
    has_baseline = not run_basis or ev.enrichment.run_approvals > 0

    # An empty device id means no device was involved (in_store/atm), not a novel one.
    if ev.customer_device_id and ev.enrichment.device_prior_approvals == 0 and has_baseline:
        evidence = (
            Evidence("customer_device_id", ev.customer_device_id),
            Evidence("device_prior_approvals", "0"),
        )
        signals.append(
            CheckResult(
                "session.device_novel",
                Verdict.CONCERN,
                "device_novel",
                (*evidence, Evidence("familiarity_basis", "run")) if run_basis else evidence,
                "this order came from a device not used for any order approved earlier in "
                "this session"
                if run_basis
                else "this order came from a device you have not used before",
            )
        )

    if ev.recent_attempt_count_10m >= VELOCITY_ATTEMPTS:
        signals.append(
            CheckResult(
                "session.velocity",
                Verdict.CONCERN,
                "velocity_elevated",
                (Evidence("recent_attempt_count_10m", str(ev.recent_attempt_count_10m)),),
                "several orders were attempted within a few minutes",
            )
        )

    if ev.enrichment.night_hours:
        signals.append(
            CheckResult(
                "session.unusual_hour",
                Verdict.CONCERN,
                "unusual_hour",
                (Evidence("timestamp_hour_utc", f"{ev.timestamp.hour:02d}"),),
                "it was placed during the night",
            )
        )

    if signals:
        return tuple(signals)
    # No reason code: a clean session must not lengthen every approval in the pack.
    return (CheckResult("session_integrity", Verdict.PASS),)
