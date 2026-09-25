"""Composition root: one place that owns the API client, the mandates, the runs and the trail.

The UI never talks to the organizers' sandbox and never holds the team key
(specs/service-contract.md §0). Everything it needs goes through here, so run state exists
once and there is nothing to keep in sync.

Each run gets a daemon thread. The client is synchronous and well tested; a thread per run is
less machinery than making the whole stack async for five scenarios, and it keeps
``tools/replay.py`` and the service on exactly the same code path.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from ..adapters.client import (
    SandboxClient,
    UpstreamError,
    UpstreamResult,
    limit_seconds,
    run_card,
    run_total,
)
from ..adapters.history import HistoryIndex
from ..adapters.parse import parse_event
from ..adapters.profile import account_ceiling, candidates
from ..audit.log import AuditLog
from ..audit.record import decision_summary
from ..compile import compile_instruction, instruction_hash, to_mandate_payload
from ..domain import deadline as guard
from ..domain import preferences as prefs
from ..domain import provenance as prov
from ..domain.amend import CONFLICT, classify
from ..domain.compose import Layer, compose
from ..domain.evaluator import evaluate
from ..domain.ledger import Ledger
from ..domain.policy import period_windows
from ..domain.settings import setting_for_facet
from .runner import ScenarioRunner
from .session import RunSession

log = logging.getLogger("leash.supervisor")

ENGINE_VERSION = os.environ.get("LEASH_ENGINE_VERSION", "leash-0.1.0")
# The customer's message on a step-up that a revoke settled — the platform records it with the
# resolution, and the audit trail keeps it beside the original step-up.
REVOKED_ANSWER = "Declined: the cardholder revoked the permission this purchase relied on."


class SupervisorError(Exception):
    """A failure with an HTTP shape: the service maps it straight to a response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Supervisor:
    def __init__(
        self,
        client: SandboxClient,
        *,
        pack: Any,
        history: HistoryIndex,
        audit: AuditLog,
        engine_version: str = ENGINE_VERSION,
    ) -> None:
        self.client = client
        self.pack = pack
        self.history = history
        self.audit = audit
        self.engine_version = engine_version
        self.sessions: dict[str, RunSession] = {}
        self.runners: dict[str, ScenarioRunner] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.mandates: dict[str, dict[str, Any]] = {}  # id -> {"ir":…, "resource":…}
        # The standing preferences layer (specs/customer-settings.md §2). One customer in this
        # prototype; in the product it is customer data held by the one app and read by the
        # engine, which is an open question in §13 rather than something we have solved.
        self._preferences: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._bootstrap: dict[str, Any] | None = None
        # (instruction sha256, mode) -> the IR the UI reviewed. Only touched under
        # `_compiling`; see `scenario_irs`.
        self._scenario_irs: dict[tuple[str, str], dict[str, Any]] = {}
        self._compiling = threading.Lock()

    # ------------------------------------------------------------------ meta

    def bootstrap(self, *, refresh: bool = False) -> dict[str, Any]:
        """Read limits from the platform rather than hard-coding them (technical_details.md)."""
        if self._bootstrap is None or refresh:
            self._bootstrap = self.client.bootstrap()
        return self._bootstrap

    def step_up_window_seconds(self) -> int:
        try:
            return limit_seconds(self.bootstrap(), "step_up_timeout_seconds") or 120
        except Exception:
            return 120

    def health(self) -> dict[str, Any]:
        try:
            upstream = self.client.health()
            reachable, detail = True, upstream
        except Exception as exc:
            reachable, detail = False, {"error": str(exc)}
        return {
            "base_url": self.client.base_url,
            "reachable": reachable,
            "mode": "replica" if "127.0.0.1" in self.client.base_url else "live",
            "upstream": detail,
        }

    # ------------------------------------------------------------------ mandates

    def compile_only(self, instruction: str, *, mode: str | None = None) -> dict[str, Any]:
        """The review screen. Compiles, submits nothing, mutates nothing.

        `mode` lets the UI pin `baseline` for a side-by-side of what the model added — the
        demo beat for "the model proposes, the guard decides" (specs/llm-compiler.md).
        """
        if not instruction.strip():
            raise SupervisorError(422, "invalid_instruction", "instruction must be non-empty")
        return compile_instruction(instruction, mode=mode)

    def scenario_irs(self, mode: str = "baseline") -> dict[str, dict[str, Any]]:
        """Every scenario's instruction compiled once, and the same IR on every later call.

        The UI boots from this list and sends the IR it showed back when it creates the
        mandate, so what the customer reviewed is what is enforced. With the model that
        needs a cache: a second call is a second, different IR, and 10–20 s each. Compiled in
        parallel (measured 2026-09-24: ten live instructions in 20 s instead of 150), and
        under one lock, so a boot that arrives while `serve-live` is warming the cache waits
        for that batch instead of paying for its own.

        Only a result the requested compiler actually produced is kept. A model call that
        failed falls back to the baseline — still a usable IR, and it says so in
        `compiler_notes` — but caching it would pin the fallback for the life of the process.
        """
        with self._compiling:
            todo = {
                sid: s["cardholder_instruction"]
                for sid, s in self.pack.scenarios.items()
                if (instruction_hash(s["cardholder_instruction"]), mode) not in self._scenario_irs
            }
            fresh: dict[str, dict[str, Any]] = {}
            if todo:
                with ThreadPoolExecutor(max_workers=len(todo)) as pool:
                    irs = pool.map(lambda text: self.compile_only(text, mode=mode), todo.values())
                    fresh = dict(zip(todo, irs, strict=True))
            for sid, ir in fresh.items():
                if "fell_back_to_baseline" not in (ir.get("compiler_notes") or []):
                    self._scenario_irs[(instruction_hash(todo[sid]), mode)] = ir
            return {
                sid: fresh.get(sid)
                or self._scenario_irs[(instruction_hash(s["cardholder_instruction"]), mode)]
                for sid, s in self.pack.scenarios.items()
            }

    def create_mandate(
        self,
        instruction: str,
        *,
        ir: dict[str, Any] | None = None,
        uncertainty_policy: str | None = None,
    ) -> dict[str, Any]:
        compiled = ir or self.compile_only(instruction)
        if uncertainty_policy:
            compiled = {**compiled, "uncertainty_policy": uncertainty_policy}
        payload = to_mandate_payload(compiled)
        # The API rejects a changed instruction and the run is lost with it — verify the round
        # trip before it leaves rather than after (specs/policy-ir.md rule 1). The hash is
        # taken at compile time, so this also catches a UI that edited the IR it was given and
        # posted it back (specs/llm-compiler.md, "Round trip").
        expected = compiled.get("instruction_sha256") or instruction_hash(instruction)
        if (
            payload["instruction"] != instruction
            or instruction_hash(payload["instruction"]) != expected
        ):
            raise SupervisorError(
                422,
                "instruction_altered",
                "the compiled mandate does not carry the instruction byte-identically",
            )
        draft = self._call(lambda: self.client.create_mandate(payload), "create_mandate")
        draft_id = str(draft.get("draft_id", ""))
        # …and verify what came back. A platform that normalises our text would otherwise
        # surface as an unexplained `instruction_mismatch` when the run refuses to start.
        echoed = draft.get("instruction")
        if isinstance(echoed, str) and instruction_hash(echoed) != expected:
            raise SupervisorError(
                409,
                "instruction_not_echoed",
                "the platform stored a different instruction than we submitted; "
                "this draft cannot start a run",
            )
        with self._lock:
            self.mandates[draft_id] = {"ir": compiled, "resource": draft}
        return {"draft_id": draft_id, "status": "draft", "ir": compiled, **draft}

    def confirm_mandate(self, draft_id: str) -> dict[str, Any]:
        confirmed = self._call(lambda: self.client.confirm_mandate(draft_id), "confirm_mandate")
        mandate_id = str(confirmed.get("mandate_id", draft_id))
        with self._lock:
            entry = self.mandates.get(draft_id, {"ir": {}})
            entry["resource"] = confirmed
            self.mandates[mandate_id] = entry
        return confirmed

    def get_mandate(self, mandate_id: str) -> dict[str, Any]:
        resource = self._call(lambda: self.client.get_mandate(mandate_id), "get_mandate")
        self._remember(mandate_id, resource)
        with self._lock:
            ir = self.mandates.get(mandate_id, {}).get("ir", {})
        return {**resource, "ir": ir}

    def list_mandates(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**entry.get("resource", {}), "ir_rules": len(entry.get("ir", {}).get("rules", []))}
                for entry in self.mandates.values()
            ]

    def patch_mandate(self, mandate_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Tighten-only, enforced by the platform. A loosening attempt is a 422 to surface."""
        resource = self._call(lambda: self.client.patch_mandate(mandate_id, body), "patch_mandate")
        self._remember(mandate_id, resource)
        return resource

    def revoke_mandate(self, mandate_id: str) -> dict[str, Any]:
        """Withdraw the permission — and answer every question still open under it.

        A step-up waiting on the customer is declined: the revoke *is* their answer, since
        they withdrew the permission the purchase relied on. Left pending, it would hold the
        platform's whole team queue for up to 120 s with nobody left to answer it
        (service-contract.md §5). The runs bound to the mandate stop first, so nothing new is
        decided under a permission being withdrawn; and the decline goes before the revoke,
        because an answer on an active mandate is specified upstream and one on a revoked
        mandate is not.
        """
        with self._lock:
            bound = [(rid, s) for rid, s in self.sessions.items() if s.mandate_id == mandate_id]
        for _, session in bound:
            session.stop_requested.set()
        declined: list[str] = []
        for run_id, session in bound:
            with session.lock:
                waiting = [a for a, s in session.step_ups.items() if s.resolved is None]
            for auth_id in waiting:
                result = self.runners[run_id].resolve(auth_id, "decline", REVOKED_ANSWER)
                if result.ok:
                    declined.append(auth_id)
                elif result.error_code == "authorization_not_pending":
                    self._settled_upstream(session, auth_id)
        resource = self._call(lambda: self.client.revoke_mandate(mandate_id), "revoke_mandate")
        self._remember(mandate_id, resource)
        return {
            **resource,
            "stopped_runs": [rid for rid, _ in bound],
            "declined_step_ups": declined,
        }

    # ------------------------------------------------------------------ settings

    def preferences(self) -> dict[str, Any]:
        """The standing layer as the customer last saved it, with every origin."""
        with self._lock:
            document = dict(self._preferences)
        return {"preferences": document, "rows": prefs.describe(document)}

    def set_preferences(self, body: dict[str, Any]) -> dict[str, Any]:
        """Replace the standing layer.

        Refusals are returned as a list rather than one at a time: the UI marks every
        offending field at once instead of discovering them over four round trips.
        """
        clean, errors = prefs.validate(body)
        if errors:
            raise SupervisorError(
                422,
                errors[0].code,
                "; ".join(f"{e.setting}: {e.message}" for e in errors),
            )
        before = prefs.to_layer(self.preferences()["preferences"])
        after = prefs.to_layer(clean)
        move = classify(
            {
                "hard_rules": list(before.hard_rules),
                "intent_facets": list(before.intent_facets),
                "uncertainty_policy": before.uncertainty_policy,
            },
            {
                "hard_rules": list(after.hard_rules),
                "intent_facets": list(after.intent_facets),
                "uncertainty_policy": after.uncertainty_policy,
            },
        )
        with self._lock:
            self._preferences = clean
        # A preference can only ever make a live mandate narrower (invariant I1), so a
        # running errand picks it up from its next authorization rather than waiting for the
        # next run. A widening of the *preferences* cannot widen a mandate either — the
        # mandate's own cap still binds — so this is safe in both directions.
        refreshed = self._refresh_sessions()
        return {
            **self.preferences(),
            "amendment": move.as_dict(),
            "runs_refreshed": refreshed,
        }

    def preference_candidates(
        self, scenario_id: str, *, mode: str | None = "baseline"
    ) -> dict[str, Any]:
        """What this scenario's cardholder profile proposes. Proposals, never rules (§8).

        Two sources. The structured fields (`budget_style`, the account ceiling) map
        deterministically in `adapters/profile.py`. `shopping_preferences` is free prose, and
        it goes through **the same compiler as an instruction** — which is the point: the
        guard already requires every proposal to quote its input verbatim
        (`llm-compiler.md` rail 2), so a candidate that cannot point at the words it came from
        does not survive. One compiler, one guard, one trust argument.

        `mode` defaults to `baseline` rather than `auto`: the UI asks for candidates on every
        boot, and a model call per boot is the mistake `service-contract.md` §2 warns about
        for `/v1/mandates/compile`. Pass `auto` to let the model read the prose.
        """
        body = candidates(self.pack, scenario_id)
        profile = body.get("customer") or {}
        prose = str(profile.get("shopping_preferences") or "").strip()
        if prose:
            body["candidates"] = [*body.get("candidates", []), *self._from_prose(prose, mode)]
        return body

    def _from_prose(self, prose: str, mode: str | None) -> list[dict[str, Any]]:
        """Compile a profile sentence into standing-setting candidates.

        Only settings the standing layer may carry survive: a profile cannot state this
        errand's object (`item_identity`, `item_attribute`), and a spend cap the compiler
        found in prose is a number the *organizers* wrote, not one the customer chose, so it
        is offered as a facet-shaped preference or not at all.
        """
        try:
            ir = compile_instruction(prose, mode=mode)
        except Exception:  # noqa: BLE001 — a candidate list must never break the screen
            log.warning("could not compile profile prose", exc_info=True)
            return []

        out: list[dict[str, Any]] = []
        for facet_obj in ir.get("intent_facets", []):
            setting = setting_for_facet(str(facet_obj.get("kind", "")))
            if setting is None or not setting.standing:
                continue
            value = dict(facet_obj.get("require") or {})
            out.append(
                {
                    "setting": setting.key,
                    "label": setting.label,
                    "value": value or True,
                    "sentence": prefs.label_for(setting.key, {setting.key: value}),
                    "source": "profile",
                    "quote": str(facet_obj.get("provenance") or ""),
                    "why": (
                        "compiled from your profile by the same guard as an instruction — it "
                        "survives only because it can quote the words it came from"
                    ),
                }
            )
        return out

    def layers(
        self, ir: dict[str, Any], scenario_id: str | None, card_id: str | None = None
    ) -> tuple[Layer, ...]:
        """Account, preferences, mandate — least to most specific (§3)."""
        stack: list[Layer] = []
        ceiling = account_ceiling(self.pack, scenario_id, card_id) if scenario_id else None
        if ceiling:
            stack.append(
                Layer(
                    source="account",
                    hard_rules=(
                        prov.stamp(
                            {
                                "field": "billing_amount_chf",
                                "operator": "<=",
                                "value": ceiling["per_transaction_limit_chf"],
                                "currency": "CHF",
                                "scope": "purchase",
                            },
                            "account",
                            ceiling["quote"],
                        ),
                    ),
                )
            )
        with self._lock:
            document = dict(self._preferences)
        stack.append(prefs.to_layer(document))
        stack.append(Layer.of("mandate", ir))
        return tuple(stack)

    def effective_policy(
        self, ir: dict[str, Any], scenario_id: str | None, card_id: str | None = None
    ) -> dict[str, Any]:
        """The policy the checks actually evaluate: every layer, tightest wins."""
        return compose(*self.layers(ir, scenario_id, card_id)).policy

    def amend_mandate(self, mandate_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """The one entry point for an edit. Narrowing applies; widening asks (§6).

        A tightening is a `PATCH`, add-only — the superseded rule stays on the record because
        it is what the customer originally consented to, and `decision-rules.md` §9 makes it
        inert. A widening is not a patch at all: it mints a draft the customer confirms, which
        is the honest reading of what a mandate is.
        """
        with self._lock:
            entry = self.mandates.get(mandate_id)
        if entry is None:
            raise SupervisorError(404, "not_found", f"no mandate {mandate_id}")
        current = entry["ir"]

        proposed = prefs.apply_settings(current, settings)
        move = classify(current, proposed)

        if move.kind == CONFLICT:
            raise SupervisorError(
                422,
                "preference_conflict",
                "; ".join(f"{c.setting}: {c.why}" for c in move.conflicts),
            )

        if move.applies_immediately:
            return self._apply_tightening(mandate_id, entry, current, proposed, move)
        return self._propose_widening(entry, current, proposed, move)

    def _apply_tightening(
        self,
        mandate_id: str,
        entry: dict[str, Any],
        current: dict[str, Any],
        proposed: dict[str, Any],
        move: Any,
    ) -> dict[str, Any]:
        # Add-only on the wire: from the platform's side a removal and a tightening look
        # identical, so sending the new cap alone is `422 rules_not_preserved`
        # (specs/service-contract.md §2). The engine then enforces the tightest of them.
        existing = list(current.get("rules") or ())
        added = [r for r in proposed.get("rules") or () if r not in existing]
        wire_rules = [
            {
                k: v
                for k, v in rule.items()
                if k in ("field", "operator", "value", "currency", "scope", "period_days")
            }
            for rule in existing + added
        ]
        patch: dict[str, Any] = {"hard_rules": wire_rules}
        if proposed.get("uncertainty_policy") != current.get("uncertainty_policy"):
            patch["uncertainty_policy"] = proposed["uncertainty_policy"]

        resource = self._call(lambda: self.client.patch_mandate(mandate_id, patch), "patch_mandate")
        # The local IR keeps the *add-only* rule list too, so the mandate a customer reviews
        # still shows what they first agreed to beside what they narrowed it to.
        stored = {**proposed, "rules": existing + added}
        stored["hard_rules"] = stored["rules"]
        with self._lock:
            entry["ir"] = stored
            entry["resource"] = {**entry.get("resource", {}), **resource}
        refreshed = self._refresh_sessions(mandate_id)
        return {
            "kind": move.kind,
            "applied": True,
            "mandate_id": mandate_id,
            "ir": stored,
            "hard_rules": resource.get("hard_rules", wire_rules),
            "amendment": move.as_dict(),
            "runs_refreshed": refreshed,
        }

    def _propose_widening(
        self,
        entry: dict[str, Any],
        current: dict[str, Any],
        proposed: dict[str, Any],
        move: Any,
    ) -> dict[str, Any]:
        instruction = str(current.get("source_instruction", ""))
        draft = self.create_mandate(instruction, ir=proposed)
        return {
            "kind": move.kind,
            "applied": False,
            "awaiting": "confirm",
            "draft_id": draft["draft_id"],
            "ir": proposed,
            "amendment": move.as_dict(),
            # The platform snapshots a mandate when a run starts, so a widening cannot reach
            # an errand already under way (specs/policy-ir.md, mandate lifecycle). Saying so
            # is better than letting the next verdict look as though the change did not take.
            "note": "confirm this to make it enforceable; it binds from the next run",
        }

    def _refresh_sessions(self, mandate_id: str | None = None) -> list[str]:
        """Recompose the policy of every live run, so an edit lands on the next order.

        A run keeps the mandate snapshot it started with upstream. Ours can move, and for a
        narrowing it should: the customer tapped Apply mid-errand and every layer here can
        only reduce what the agent may do (I1). Widenings never reach this method — they go
        through a fresh draft.
        """
        with self._lock:
            sessions = [
                s
                for s in self.sessions.values()
                if mandate_id is None or s.mandate_id == mandate_id
            ]
            irs = {s.run_id: self.mandates.get(s.mandate_id, {}).get("ir", {}) for s in sessions}
        touched: list[str] = []
        for session in sessions:
            policy = self.effective_policy(
                irs[session.run_id], session.scenario_id, session.card_id
            )
            with session.lock:
                session.policy = policy
                session.ir = irs[session.run_id]
                session.period_windows = period_windows(policy["hard_rules"])
            touched.append(session.run_id)
        return touched

    # ------------------------------------------------------------------ runs

    def start_run(
        self,
        scenario_id: str,
        *,
        mandate_id: str | None = None,
        auto_resolve: str | None = None,
    ) -> RunSession:
        scenario = self.pack.scenarios.get(scenario_id)
        if scenario is None:
            raise SupervisorError(404, "unknown_scenario", f"no scenario {scenario_id}")

        if mandate_id is None:
            # The one-call path (contract §3): compile the scenario's own instruction, create
            # and confirm. A convenience, not a shortcut past consent — the response names
            # the mandate that was confirmed.
            draft = self.create_mandate(scenario["cardholder_instruction"])
            mandate_id = str(self.confirm_mandate(draft["draft_id"])["mandate_id"])

        with self._lock:
            ir = self.mandates.get(mandate_id, {}).get("ir", {})
        run = self._call(lambda: self.client.start_run(scenario_id, str(mandate_id)), "start_run")
        run_id = str(run["run_id"])
        card_id = run_card(run) or self._run_card(run_id)
        if account_ceiling(self.pack, scenario_id, card_id) is None:
            log.warning(
                "run %s: card %s has no account in the pack; judged without the account layer",
                run_id,
                card_id,
            )

        # The snapshot every decision in this run is judged against: account ceiling,
        # standing preferences, and the mandate the customer confirmed, composed
        # tightest-wins (specs/customer-settings.md §3). `provenance` travels with it —
        # stripping it would leave the customer message unable to say *which* limit bound,
        # which with more than one layer is the difference between an explanation and an
        # apparent contradiction (§5).
        policy = self.effective_policy(ir, scenario_id, card_id)
        session = RunSession(
            run_id=run_id,
            scenario_id=scenario_id,
            mandate_id=str(mandate_id),
            policy=policy,
            period_windows=period_windows(policy["hard_rules"]),
            # The real API reports no total — it generates each event only once the one before
            # is settled — so the catalogue's count stands in. Without it the UI read "0 / 0".
            total=run_total(run) or int(scenario["event_count"]),
            ir=ir,
            auto_resolve=auto_resolve,
            card_id=card_id,
        )
        runner = ScenarioRunner(
            self.client,
            session,
            history=self.history,
            merchants=self.pack.merchants,
            audit=self.audit,
            engine_version=self.engine_version,
            step_up_window_seconds=self.step_up_window_seconds(),
            dispatch=self._runner_for,
        )
        thread = threading.Thread(target=runner.run, name=f"run-{run_id}", daemon=True)
        with self._lock:
            self.sessions[run_id] = session
            self.runners[run_id] = runner
            self.threads[run_id] = thread
        thread.start()
        return session

    def _run_card(self, run_id: str) -> str | None:
        """The run's card from its resource, when the start response did not name it.

        Only the account layer depends on it, and a run without that layer is still judged by
        every other one — so a failed read costs the ceiling, never the run.
        """
        try:
            return run_card(self.client.get_run(run_id))
        except Exception:  # noqa: BLE001
            log.warning("could not read run %s for its card", run_id, exc_info=True)
            return None

    def _runner_for(self, run_id: str) -> ScenarioRunner | None:
        """Which loop owns a delivered authorization. The queue is team-global, not per-run."""
        with self._lock:
            return self.runners.get(run_id)

    def session(self, run_id: str) -> RunSession:
        with self._lock:
            session = self.sessions.get(run_id)
        if session is None:
            raise SupervisorError(404, "not_found", f"no run {run_id}")
        return session

    def stop_run(self, run_id: str) -> RunSession:
        session = self.session(run_id)
        session.stop_requested.set()
        return session

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self.sessions.values())
        return [s.as_dict(include_decisions=False) for s in sessions]

    def decisions(self, run_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            sessions = (
                [self.sessions[run_id]]
                if run_id and run_id in self.sessions
                else list(self.sessions.values())
            )
        rows = [d for s in sessions for d in s.decisions]
        return list(reversed(rows))[:limit]

    # ------------------------------------------------------------------ step-ups

    def step_ups(self, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self.sessions.values())
        out: list[dict[str, Any]] = []
        for session in sessions:
            with session.lock:
                out.extend(
                    s.as_dict()
                    for s in session.step_ups.values()
                    if include_resolved or s.resolved is None
                )
        return sorted(out, key=lambda s: str(s["asked_at"]))

    def resolve_step_up(
        self, authorization_id: str, outcome: str, message: str = ""
    ) -> dict[str, Any]:
        if outcome not in ("approve", "decline"):
            raise SupervisorError(
                422, "invalid_resolution", "resolution must be approve or decline"
            )
        with self._lock:
            owner = next(
                ((rid, s) for rid, s in self.sessions.items() if authorization_id in s.step_ups),
                None,
            )
        if owner is None:
            raise SupervisorError(
                404, "not_found", f"no step-up awaiting resolution for {authorization_id}"
            )
        run_id, session = owner
        if session.step_ups[authorization_id].resolved is not None:
            raise SupervisorError(409, "already_resolved", "a human has already answered this")

        result: UpstreamResult = self.runners[run_id].resolve(authorization_id, outcome, message)
        if result.error_code == "authorization_not_pending":
            self._settled_upstream(session, authorization_id)
        if not result.ok:
            raise SupervisorError(
                502 if result.status is None else result.status,
                result.error_code or "upstream_error",
                result.error_message or "the sandbox refused the resolution",
            )
        return {
            "authorization_id": authorization_id,
            "status": "approved" if outcome == "approve" else "declined",
            "resolved_by": "customer",
            "run_id": run_id,
            "window": session.window(),
        }

    def _settled_upstream(self, session: RunSession, authorization_id: str) -> None:
        """The platform settled a step-up before the customer's answer reached it.

        On the real API that is almost always the 120-second window: it declines the purchase
        itself (`decision_source: "timeout"`) and answers a late `/resolve` with 409
        `authorization_not_pending`. The answer is still refused — it was not applied — but
        the step-up must stop reading as waiting, or the phone keeps asking a question the
        platform has already closed.
        """
        try:
            row = next(
                (
                    r
                    for r in self.client.authorizations()
                    if r.get("authorization_id") == authorization_id
                ),
                {},
            )
        except Exception:
            row = {}
        approved = row.get("status") == "approved"
        # Unreadable counts as the timeout: it is by far the likeliest cause, and a decline is
        # the direction that cannot overstate approved spend.
        timed_out = row.get("decision_source", "timeout") == "timeout"
        with session.lock:
            step_up = session.step_ups.get(authorization_id)
            if step_up is not None and step_up.resolved is None:
                step_up.resolved = (
                    "expired"
                    if timed_out and not approved
                    else "approve"
                    if approved
                    else "decline"
                )
            session.ledger.resolve(authorization_id, approved)

    # ------------------------------------------------------------------ what-if

    def decide(
        self,
        event: dict[str, Any],
        *,
        policy: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate one event and submit nothing. Contract §4.

        With a ``run_id`` the run's ledger and seen events are read — never written — so the
        window figures match what the live loop saw at that point.
        """
        if not isinstance(event, dict) or "authorization" not in event:
            raise SupervisorError(
                400, "invalid_event", "body.event must be a complete authorization.request"
            )

        session = self.sessions.get(run_id) if run_id else None
        mandate = event.get("mandate", {})
        if policy is not None:
            effective = policy  # the explicit what-if lever: take it exactly as given
        elif session is not None:
            # The run's own composed snapshot, so a what-if answers with the same layers the
            # live loop is judging against rather than the mandate alone.
            effective = dict(session.policy)
        else:
            effective = self.effective_policy(
                {
                    "rules": mandate.get("hard_rules", []),
                    "uncertainty_policy": mandate.get("uncertainty_policy", "ask"),
                    "intent_facets": [],
                },
                None,
            )
        windows = session.period_windows if session else period_windows(effective["hard_rules"])
        try:
            ev = parse_event(
                event,
                history=self.history,
                ledger=session.ledger if session else Ledger(),
                merchants=self.pack.merchants,
                period_windows=windows,
                seen_in_run=list(session.seen) if session else [],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SupervisorError(
                400, "invalid_event", f"could not parse the event: {type(exc).__name__}: {exc}"
            ) from exc

        now = datetime.now(UTC)
        record = evaluate(ev, effective, engine_version=self.engine_version, now=now)
        summary = decision_summary(record, ev)
        deadline_at = guard.parse_deadline(event)
        summary["timing"]["budget_ms"] = guard.budget_ms(deadline_at, now)
        summary["timing"]["guard_tripped"] = False
        primary = ev.enrichment.primary_window()
        summary["window"] = {
            "period_days": primary[0] if primary else None,
            "approved_spend_chf": str(primary[1]) if primary else None,
            "windows": [
                {"period_days": days, "approved_spend_chf": str(spend)}
                for days, spend in sorted(ev.enrichment.approved_spend_windows.items())
            ],
        }
        summary["submitted"] = False
        return summary

    # ------------------------------------------------------------------ internals

    def _remember(self, mandate_id: str, resource: dict[str, Any]) -> None:
        """Write what the platform just said back into the list the phone reads.

        `list_mandates` answers from this cache, not upstream, so a mutation that skips it
        leaves `GET /v1/mandates` showing a revoked leash as active while the detail view
        says revoked. Only mandates created here are listed; one we never made stays out.
        """
        with self._lock:
            entry = self.mandates.get(mandate_id)
            if entry is not None:
                entry["resource"] = {**entry.get("resource", {}), **resource}

    def _call(self, fn: Any, what: str) -> dict[str, Any]:
        """Turn an upstream HTTP failure into a SupervisorError the service can render."""
        try:
            return dict(fn())
        except UpstreamError as exc:
            if exc.status is None:
                raise SupervisorError(
                    502, "upstream_unreachable", f"{what}: {self.client.base_url} — {exc}"
                ) from exc
            raise SupervisorError(exc.status, exc.code, exc.detail or f"{what} failed") from exc
        # The transport can still raise its own connection/timeout error before a response
        # exists; that is unreachable, whichever library the injected client came from.
        except Exception as exc:
            raise SupervisorError(
                502, "upstream_unreachable", f"{what}: {self.client.base_url} — {exc}"
            ) from exc


def _period_windows(ir: dict[str, Any]) -> tuple[int, ...]:
    """Every window enrichment must compute for this mandate. §9."""
    return period_windows(ir.get("rules", []))
