"""The compiler falls back to the baseline on any model failure at all.

Spec: specs/llm-compiler.md, "Fallback". The challenge asks what happens when optional models
fail; the answer has to be demonstrable, not asserted. This whole file runs with no API key —
which is also the state the repository is developed in, so the fallback path is the one that
gets exercised on every `make check`, not a branch nobody takes until it matters.

The provider is OpenRouter by default, or Swisscom's Apertus, both reached over plain HTTP.
The stubs below stand in for that one POST, so every branch is reachable without a network and
without a key.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from leash.compile import compile_instruction, compiler_mode
from leash.compile import llm as llm_compiler
from leash.compile.baseline import compile_instruction as baseline_compile

ROOT = Path(__file__).resolve().parents[2]
INSTRUCTION = (
    "Order our household groceries for delivery. Keep each order at or below CHF 120 "
    "including delivery, and keep the total across any seven days at or below CHF 300. "
    "Ask me when uncertain."
)


@pytest.fixture(autouse=True)
def _default_provider(monkeypatch: Any) -> None:
    """Every test starts on OpenRouter, whatever the shell exported; Apertus tests opt in."""
    monkeypatch.delenv("LEASH_COMPILER_PROVIDER", raising=False)


class _Stub:
    """Stands in for the OpenRouter HTTP client. Used as a context manager, like the real one."""

    def __init__(self, *, raises: Exception | None = None, response: Any = None) -> None:
        self.raises = raises
        self.response = response
        self.sent: dict[str, Any] | None = None

    def __enter__(self) -> _Stub:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post(self, _url: str, json: dict[str, Any] | None = None, **_: Any) -> Any:
        self.sent = json
        if self.raises is not None:
            raise self.raises
        return self.response


class _Response:
    """A minimal httpx.Response stand-in: status, and a body that may not be JSON."""

    def __init__(self, status_code: int = 200, body: Any = None, text: str | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self._text = text

    def json(self) -> Any:
        if self._text is not None:
            raise ValueError("not JSON")
        return self._body


def completion(content: str, *, finish_reason: str = "stop", model: str = "test/model") -> Any:
    """An OpenRouter chat-completion envelope carrying `content`."""
    return _Response(
        body={
            "model": model,
            "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        }
    )


def assert_is_baseline(ir: dict[str, Any], reason_prefix: str) -> None:
    """The fallback is *total*: the IR is the baseline's, whole, and says why."""
    expected = baseline_compile(INSTRUCTION)
    assert ir["compiler"] == "baseline-deterministic"
    assert ir["rules"] == expected["rules"]
    assert ir["intent_facets"] == expected["intent_facets"]
    assert ir["source_instruction"] == INSTRUCTION
    assert "fell_back_to_baseline" in ir["compiler_notes"]
    assert any(n.startswith(reason_prefix) for n in ir["compiler_notes"]), ir["compiler_notes"]


# --------------------------------------------------------------- the key is not there


def test_no_credentials_at_all_still_compiles_a_usable_mandate(monkeypatch: Any) -> None:
    """The headline case: nothing configured, and policy authoring still works."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("LEASH_COMPILER", "auto")

    ir = compile_instruction(INSTRUCTION)
    assert_is_baseline(ir, "model_unavailable")
    # Usable, not merely present: both of SCEN0001's caps are there to enforce.
    assert {(r["scope"], r["value"]) for r in ir["rules"]} == {
        ("purchase", 120.0),
        ("period", 300.0),
    }


def test_the_key_being_absent_short_circuits_before_any_request(monkeypatch: Any) -> None:
    """No key means no call at all — not a call that fails slowly at a hackathon."""

    def explode(_: float) -> Any:
        raise AssertionError("no request may be made without a key")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(llm_compiler, "_client", explode)
    assert_is_baseline(llm_compiler.compile_instruction(INSTRUCTION), "model_unavailable")


# --------------------------------------------------------------- the call goes wrong


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("network is unreachable"),
        RuntimeError("something nobody predicted"),
    ],
    ids=["connect-timeout", "read-timeout", "network", "unexpected"],
)
def test_every_transport_error_falls_back(monkeypatch: Any, exc: Exception) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(llm_compiler, "_client", lambda _t: _Stub(raises=exc))
    assert_is_baseline(llm_compiler.compile_instruction(INSTRUCTION), "model_error")


@pytest.mark.parametrize("status", [401, 402, 404, 429, 500, 503])
def test_every_http_error_falls_back(monkeypatch: Any, status: int) -> None:
    """402 is OpenRouter's out-of-credit. On a hackathon budget, plan for it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(
        llm_compiler, "_client", lambda _t: _Stub(response=_Response(status_code=status))
    )
    assert_is_baseline(llm_compiler.compile_instruction(INSTRUCTION), f"model_error: http_{status}")


def test_an_error_envelope_returned_with_200_falls_back(monkeypatch: Any) -> None:
    """OpenRouter reports some upstream failures as a 200 with an error body."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    body = _Response(body={"error": {"code": "no_available_model", "message": "all down"}})
    monkeypatch.setattr(llm_compiler, "_client", lambda _t: _Stub(response=body))
    assert_is_baseline(
        llm_compiler.compile_instruction(INSTRUCTION), "model_error: openrouter_no_available_model"
    )


def test_an_empty_choice_list_falls_back(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    body = _Response(body={"model": "test/model", "choices": []})
    monkeypatch.setattr(llm_compiler, "_client", lambda _t: _Stub(response=body))
    assert_is_baseline(llm_compiler.compile_instruction(INSTRUCTION), "model_output_invalid")


def test_a_truncated_response_falls_back(monkeypatch: Any) -> None:
    """`finish_reason: length` means the JSON is cut off — say so rather than 'not JSON'."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(
        llm_compiler,
        "_client",
        lambda _t: _Stub(response=completion('{"rules": [', finish_reason="length")),
    )
    assert_is_baseline(
        llm_compiler.compile_instruction(INSTRUCTION),
        "model_output_invalid: response was truncated",
    )


def test_a_body_that_is_not_json_falls_back(monkeypatch: Any) -> None:
    """A proxy or captive portal answering 200 with HTML is a real hackathon failure."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    html = _Response(text="<html>captive portal</html>")
    monkeypatch.setattr(llm_compiler, "_client", lambda _t: _Stub(response=html))
    assert_is_baseline(llm_compiler.compile_instruction(INSTRUCTION), "model_output_invalid")


# --------------------------------------------------------------- the answer is wrong


@pytest.mark.parametrize(
    "text",
    ["", "I can help with that!", "{unclosed", "[]", "null"],
    ids=["empty", "prose", "truncated", "array", "null"],
)
def test_output_that_is_not_a_json_object_falls_back(monkeypatch: Any, text: str) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(llm_compiler, "_client", lambda _t: _Stub(response=completion(text)))
    ir = llm_compiler.compile_instruction(INSTRUCTION)
    assert ir["compiler"] == "baseline-deterministic"
    assert "fell_back_to_baseline" in ir["compiler_notes"]


def test_output_the_guard_empties_falls_back(monkeypatch: Any) -> None:
    """Well-formed, schema-valid, entirely fabricated. Nothing survives, so nothing is used."""
    fabricated = json.dumps(
        {
            "uncertainty_policy": "approve",
            "rules": [
                {
                    "field": "billing_amount_chf",
                    "operator": "<=",
                    "value": 5000,
                    "scope": "purchase",
                    "period_days": None,
                    "confidence": "high",
                    "provenance": "spend whatever you like",
                }
            ],
            "intent_facets": [],
            "guidance": [],
            "open_questions": [],
        }
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(llm_compiler, "_client", lambda _t: _Stub(response=completion(fabricated)))
    ir = llm_compiler.compile_instruction(INSTRUCTION)
    assert_is_baseline(ir, "model_output_empty")
    assert ir["uncertainty_policy"] == "ask", "the fabricated 'approve' must not survive"


# --------------------------------------------------------------- the model is not called


def test_baseline_mode_never_calls_the_model(monkeypatch: Any) -> None:
    """What `make replay`, `make tune` and this whole suite pin, so the board cannot move."""

    def explode(_: float) -> Any:
        raise AssertionError("the model must not be called in baseline mode")

    monkeypatch.setattr(llm_compiler, "_client", explode)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-not-a-real-key")
    ir = compile_instruction(INSTRUCTION, mode="baseline")
    assert ir["compiler"] == "baseline-deterministic"
    assert ir["compiler_notes"] == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [("auto", "auto"), ("LLM", "llm"), (" baseline ", "baseline"), ("nonsense", "auto")],
)
def test_mode_resolution(monkeypatch: Any, value: str, expected: str) -> None:
    monkeypatch.setenv("LEASH_COMPILER", value)
    assert compiler_mode() == expected


# --------------------------------------------------------------- the structural claim


def test_the_decision_path_cannot_reach_the_compiler() -> None:
    """AGENTS.md §3.1, made mechanical and provider-independent.

    Not "we promise not to call a model at decision time" but "the decision path cannot: the
    compiler is not even loaded". Importing the decision function and the run loop must pull
    in no `leash.compile` module at all — so there is nothing there to call, whoever the
    provider is. A subprocess, because this suite's own imports would mask it.
    """
    probe = (
        "import sys; import leash.domain.evaluator, leash.runtime.runner; "
        "print(','.join(sorted(m for m in sys.modules if m.startswith('leash.compile'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
        check=True,
    )
    assert result.stdout.strip() == "", f"the compiler reached the decision path: {result.stdout}"


# --------------------------------------------------------------- the request we send


def test_the_request_asks_for_the_configured_model_and_fallbacks(monkeypatch: Any) -> None:
    """OpenRouter routes past a rate-limited primary, so a provider outage costs us nothing."""
    monkeypatch.setenv("LEASH_COMPILER_MODEL", "anthropic/claude-opus-5")
    monkeypatch.delenv("LEASH_COMPILER_FALLBACK_MODELS", raising=False)
    body = llm_compiler.request_body(INSTRUCTION)

    assert body["model"] == "anthropic/claude-opus-5"
    assert body["models"][0] == "anthropic/claude-opus-5", "the primary is tried first"
    assert len(body["models"]) > 1, "and there is somewhere to route to"
    assert len(set(body["models"])) == len(body["models"]), "no model listed twice"


def test_the_request_is_deterministic_and_schema_constrained() -> None:
    """A compiler should compile the same text the same way."""
    body = llm_compiler.request_body(INSTRUCTION)
    assert body["temperature"] == 0
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] is llm_compiler.OUTPUT_SCHEMA


def test_the_request_carries_the_instruction_and_nothing_about_a_purchase() -> None:
    """The compiler's only input is the cardholder's own words (specs/llm-compiler.md §1)."""
    body = llm_compiler.request_body(INSTRUCTION)
    user = next(m["content"] for m in body["messages"] if m["role"] == "user")
    assert INSTRUCTION in user
    # Nothing merchant-controlled can be in a prompt built from one string.
    assert set(body) <= {
        "model",
        "models",
        "temperature",
        "max_tokens",
        "messages",
        "response_format",
    }


def test_a_configured_fallback_list_is_honoured(monkeypatch: Any) -> None:
    monkeypatch.setenv("LEASH_COMPILER_MODEL", "openai/gpt-5.6-terra")
    monkeypatch.setenv("LEASH_COMPILER_FALLBACK_MODELS", "google/gemini-3.8-flash")
    assert llm_compiler.models() == ["openai/gpt-5.6-terra", "google/gemini-3.8-flash"]


def test_fallback_routing_can_be_switched_off(monkeypatch: Any) -> None:
    """An empty list means "this model or the baseline" — the strictest reading."""
    monkeypatch.setenv("LEASH_COMPILER_MODEL", "anthropic/claude-opus-5")
    monkeypatch.setenv("LEASH_COMPILER_FALLBACK_MODELS", "")
    assert llm_compiler.models() == ["anthropic/claude-opus-5"]
    assert "models" not in llm_compiler.request_body(INSTRUCTION)


def test_the_ir_names_the_model_that_actually_answered(monkeypatch: Any) -> None:
    """If OpenRouter routed past the primary, the audit trail must say which model compiled."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    answer = json.dumps(
        {
            "uncertainty_policy": "ask",
            "rules": [
                {
                    "field": "billing_amount_chf",
                    "operator": "<=",
                    "value": 120,
                    "scope": "purchase",
                    "period_days": None,
                    "confidence": "high",
                    "provenance": "CHF 120",
                }
            ],
            "intent_facets": [],
            "guidance": [],
            "open_questions": [],
        }
    )
    monkeypatch.setattr(
        llm_compiler,
        "_client",
        lambda _t: _Stub(response=completion(answer, model="anthropic/claude-sonnet-5")),
    )
    ir = llm_compiler.compile_instruction(INSTRUCTION)
    assert ir["compiler"] == "llm:anthropic/claude-sonnet-5"


# --------------------------------------------------------------- Apertus (Swisscom)


def apertus(monkeypatch: Any, *, key: str | None = "swisscom-test") -> None:
    monkeypatch.setenv("LEASH_COMPILER_PROVIDER", "apertus")
    monkeypatch.delenv("APERTUS_MODEL", raising=False)
    monkeypatch.delenv("APERTUS_BASE_URL", raising=False)
    if key is None:
        monkeypatch.delenv("SWISSCOM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SWISSCOM_API_KEY", key)


CAP_ONLY = json.dumps(
    {
        "uncertainty_policy": "ask",
        "rules": [
            {
                "field": "billing_amount_chf",
                "operator": "<=",
                "value": 120,
                "scope": "purchase",
                "period_days": None,
                "confidence": "high",
                "provenance": "CHF 120",
            }
        ],
        "intent_facets": [],
        "guidance": [],
        "open_questions": [],
    }
)


def test_openrouter_stays_the_default_and_its_request_is_unchanged() -> None:
    """Adding a provider must not move a byte of what OpenRouter is sent."""
    assert llm_compiler.provider().name == "openrouter"
    body = llm_compiler.request_body(INSTRUCTION)
    assert body["messages"][0]["content"] == llm_compiler.SYSTEM_PROMPT
    assert body["max_tokens"] == 32000
    assert "models" in body


def test_an_unknown_provider_name_means_openrouter(monkeypatch: Any) -> None:
    monkeypatch.setenv("LEASH_COMPILER_PROVIDER", "apertos")
    assert llm_compiler.provider().name == "openrouter"


def test_apertus_asks_for_its_one_model_with_the_schema_in_the_prompt(monkeypatch: Any) -> None:
    """No routing array, and the model reads the schema's descriptions in the prompt itself."""
    apertus(monkeypatch)
    monkeypatch.setenv("LEASH_COMPILER_MODEL", "google/gemini-3.7-flash")  # OpenRouter's, unread
    body = llm_compiler.request_body(INSTRUCTION)

    assert body["model"] == "swiss-ai/Apertus-v1.5-70B"
    assert "models" not in body, "only OpenRouter routes"
    assert body["temperature"] == 0
    # Grammar-enforced, Apertus padded the JSON with whitespace until max_tokens (2026-09-24).
    assert "response_format" not in body
    system = body["messages"][0]["content"]
    assert system.startswith(llm_compiler.SYSTEM_PROMPT)
    assert "Anything that is not a literal substring" in system, "provenance rule reaches it"
    assert "- merchant_familiarity: prior_approvals_min" in system, "which keys each kind reads"


def test_apertus_never_asks_for_more_than_the_gateway_grants_per_minute(monkeypatch: Any) -> None:
    """The gateway reserves max_tokens against 12 500 output tokens a minute, up front.

    Over that, every request is a 429 before the model runs (measured 2026-09-24 at 16 000).
    """
    apertus(monkeypatch)
    assert llm_compiler.request_body(INSTRUCTION)["max_tokens"] <= 12500


def test_apertus_model_and_url_can_be_overridden(monkeypatch: Any) -> None:
    apertus(monkeypatch)
    monkeypatch.setenv("APERTUS_MODEL", "swiss-ai/Apertus-v2-70B")
    monkeypatch.setenv("APERTUS_BASE_URL", "https://example.test/v1")
    assert llm_compiler.models() == ["swiss-ai/Apertus-v2-70B"]
    assert llm_compiler.provider().url() == "https://example.test/v1"


def test_apertus_client_sends_its_key_and_none_of_openroutes_headers(monkeypatch: Any) -> None:
    apertus(monkeypatch)
    with llm_compiler._client(5.0) as client:
        assert str(client.base_url).rstrip("/") == llm_compiler.APERTUS_BASE_URL
        assert client.headers["Authorization"] == "Bearer swisscom-test"
        assert "HTTP-Referer" not in client.headers
        assert "X-Title" not in client.headers


def test_apertus_without_its_key_names_that_key_and_makes_no_request(monkeypatch: Any) -> None:
    """An OpenRouter key must not satisfy Apertus: that would send the wrong secret."""

    def explode(_: float) -> Any:
        raise AssertionError("no request may be made without the provider's key")

    apertus(monkeypatch, key=None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(llm_compiler, "_client", explode)
    assert_is_baseline(
        llm_compiler.compile_instruction(INSTRUCTION), "model_unavailable: SWISSCOM_API_KEY"
    )
    monkeypatch.setenv("LEASH_COMPILER", "auto")
    assert_is_baseline(compile_instruction(INSTRUCTION), "model_unavailable: SWISSCOM_API_KEY")


def test_an_expired_apertus_key_falls_back(monkeypatch: Any) -> None:
    """Keymaker keys last 60 minutes; the 401 after that must cost the baseline, not the run."""
    apertus(monkeypatch)
    monkeypatch.setattr(
        llm_compiler, "_client", lambda _t: _Stub(response=_Response(status_code=401))
    )
    assert_is_baseline(llm_compiler.compile_instruction(INSTRUCTION), "model_error: http_401")


def test_swisscoms_string_error_envelope_falls_back(monkeypatch: Any) -> None:
    """The gateway's shape, observed 2026-09-24: `error` is a string, the code sits beside it."""
    apertus(monkeypatch)
    body = _Response(
        body={
            "status": "401",
            "code": "INVALID_AUTHENTICATION_CREDENTIALS",
            "error": "invalid_client",
        }
    )
    monkeypatch.setattr(llm_compiler, "_client", lambda _t: _Stub(response=body))
    assert_is_baseline(
        llm_compiler.compile_instruction(INSTRUCTION),
        "model_error: apertus_INVALID_AUTHENTICATION_CREDENTIALS",
    )


def test_apertus_names_itself_on_the_ir(monkeypatch: Any) -> None:
    apertus(monkeypatch)
    monkeypatch.setattr(
        llm_compiler,
        "_client",
        lambda _t: _Stub(response=completion(CAP_ONLY, model="swiss-ai/Apertus-v1.5-70B")),
    )
    ir = llm_compiler.compile_instruction(INSTRUCTION)
    assert ir["compiler"] == "llm:swiss-ai/Apertus-v1.5-70B"


@pytest.mark.parametrize(
    "wrapped",
    [f"```json\n{CAP_ONLY}\n```", f"```\n{CAP_ONLY}\n```\n", f"  {CAP_ONLY}  "],
    ids=["json-fence", "bare-fence", "whitespace"],
)
def test_a_fenced_reply_is_unwrapped_then_guarded_as_usual(monkeypatch: Any, wrapped: str) -> None:
    """A Markdown fence is packaging. What is inside still has to survive the guard."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(llm_compiler, "_client", lambda _t: _Stub(response=completion(wrapped)))
    ir = llm_compiler.compile_instruction(INSTRUCTION)
    assert ir["compiler"] == "llm:test/model"


def test_the_model_path_hands_the_guard_the_catalogue(monkeypatch: Any) -> None:
    """Rail 5b only works if llm.py passes the catalogue through; this is the wire."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    answer = json.loads(CAP_ONLY)
    answer["intent_facets"] = [
        {
            "kind": "item_identity",
            "require": {
                "item_category_in": ["groceries"],
                "item_keywords_all": ["groceries", "household"],
                "item_description": None,
            },
            "confidence": "high",
            "provenance": "household groceries",
            "open_question": None,
        }
    ]
    monkeypatch.setattr(
        llm_compiler, "_client", lambda _t: _Stub(response=completion(json.dumps(answer)))
    )
    ir = llm_compiler.compile_instruction(INSTRUCTION)
    assert ir["intent_facets"][0]["require"] == {"item_category_in": ["groceries"]}
