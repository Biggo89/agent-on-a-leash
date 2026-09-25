"""Request bodies for the UI-facing service. See specs/service-contract.md.

Deliberately permissive on the way in and explicit on the way out: the frontend is being
built in parallel against this contract, and a 422 on a field nobody agreed on is a worse
failure at hour 20 than an ignored extra key.

`PatchMandateRequest` is the one exception, and the reason is worth stating: every one of its
fields is optional, so an unrecognised body does not degrade into "the meaningful fields still
arrived" — it degrades into an **empty patch that returns 200 and changes nothing**. A
frontend patching the create-shape (`{"instruction": …}`) would be told its tightening
succeeded when the mandate never moved. That is not an ignored extra key, it is a silent
failure reported as success, so it is refused instead.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CompileRequest(BaseModel):
    instruction: str = Field(..., min_length=1, description="The cardholder's own words")
    mode: Literal["auto", "baseline", "llm"] | None = Field(
        None,
        description=(
            "Which compiler to use. Omit for the configured default. `baseline` pins the "
            "deterministic one — the UI uses it for the side-by-side that shows what the "
            "model added and what the guard refused (specs/llm-compiler.md)."
        ),
    )


class MandateRequest(BaseModel):
    instruction: str = Field(..., min_length=1)
    uncertainty_policy: Literal["ask", "decline", "approve"] | None = None
    ir: dict[str, Any] | None = Field(
        None, description="An edited Policy IR from the review screen; compiled fresh if absent"
    )


class ConfirmRequest(BaseModel):
    confirmed: bool = True


class PatchMandateRequest(BaseModel):
    """Tighten-only. Omitted fields are preserved by the platform.

    Unknown keys are refused rather than ignored — see the module docstring. To change the
    rules, send `hard_rules`; there is deliberately no `instruction` field, because a mandate
    is tightened against its stored rules, not recompiled from new text.
    """

    model_config = ConfigDict(extra="forbid")

    hard_rules: list[dict[str, Any]] | None = None
    uncertainty_policy: Literal["ask", "decline", "approve"] | None = None
    guidance: list[str] | None = None
    open_questions: list[str] | None = None


class PreferencesRequest(BaseModel):
    """The standing preferences layer — specs/customer-settings.md §2.

    Deliberately permissive here and strict in `domain.preferences.validate`, which refuses
    per-field and reports **every** offending key at once: the UI marks them all in one pass
    rather than discovering them over four round trips. A `PATCH`-style partial is not
    offered, because a preferences screen shows the whole document and saving it whole is the
    only way "I cleared that setting" and "I did not send that setting" stay distinguishable.
    """

    model_config = ConfigDict(extra="allow")


class AmendRequest(BaseModel):
    """One edit to a confirmed mandate, in the settings vocabulary.

    Same shape as a preferences document on purpose: one vocabulary for the standing layer and
    the per-errand one means one editor in the UI and one classifier here. `settings` may
    carry any key from `GET /v1/config`'s `settings` table; the engine decides whether it
    narrows or widens, and routes accordingly (§6).
    """

    settings: dict[str, Any] = Field(
        ..., description="setting key → value, e.g. {'per_order_limit_chf': 250}"
    )


class RunRequest(BaseModel):
    scenario_id: str
    mandate_id: str | None = Field(
        None, description="Omit to compile+create+confirm the scenario's own instruction"
    )
    auto_resolve: Literal["approve", "decline"] | None = Field(
        None, description="Demo aid: answer every step-up automatically. Default is a real human."
    )


class DecideRequest(BaseModel):
    event: dict[str, Any] = Field(..., description="A complete authorization.request")
    policy: dict[str, Any] | None = Field(
        None, description="Overrides the mandate snapshot on the event — the what-if lever"
    )
    run_id: str | None = Field(None, description="Read this run's ledger for window figures")


class ResolveRequest(BaseModel):
    decision: Literal["approve", "decline"]
    customer_message: str = ""
