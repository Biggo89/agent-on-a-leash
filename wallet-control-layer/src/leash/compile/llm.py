"""The model-backed policy compiler — the one place in this codebase that calls an LLM.

Instruction in, Policy IR out, once, at mandate creation, with a human reading the result
before it binds. No deadline, no merchant text, no path back from the decision engine.
Spec: specs/llm-compiler.md. Architectural invariant: AGENTS.md §3.1.

**Providers: OpenRouter (default) or Swisscom's Apertus**, chosen by `LEASH_COMPILER_PROVIDER`.
Both speak the OpenAI chat-completions dialect, so both go over plain HTTP with `httpx` —
already a core dependency, so the compiler adds none. We use no SDK features here: one JSON
POST, no streaming, no tool loop. That also keeps the port to TypeScript a `fetch` call rather
than a second SDK to learn, which is the portability contract in GUIDELINES.md.

Everything the model returns goes through `contract.accept()` before it becomes an IR, and any
failure at all returns the deterministic baseline instead. Both properties are tested with no
API key present, which is also how this module is developed.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..adapters.datapack import item_catalogue, merchant_places
from ..domain.settings import category_vocabulary
from . import baseline as baseline_compiler
from .contract import (
    FACET_REQUIRE_KEYS,
    ITEM_CATEGORIES,
    MERCHANT_CATEGORIES,
    accept,
    apply_safety_floor,
)

log = logging.getLogger("leash.compile")

BASE_URL = "https://openrouter.ai/api/v1"

# Chosen by measurement, not by tier. Benchmarked on the five pack instructions 2026-09-08,
# scoring which intent facets each model recovered against what the 45-decision board needs:
#
#   google/gemini-3.7-flash        0 missed, 0 invented   58 s   $0.043   <- this one
#   anthropic/claude-opus-5        0 missed, 1 invented   64 s   $0.209    5x the cost
#   anthropic/claude-sonnet-5      1 missed, 2 invented   77 s   $0.098    read "27-inch" as a
#                                                                         clothing size
#   google/gemini-3.5-flash-lite   2 missed, 1 invented   10 s   $0.009    lost merchant_type
#                                                                         on SCEN0002
#
# The cheap tier is genuinely fast but drops facets the board depends on, and the expensive
# tier is not more accurate — so the middle is both the best and nearly the cheapest.
#
# A NON-REASONING model is not required, but a reasoning one needs the token budget below:
# at max_tokens=8000 `qwen/qwen3.8-flash` spent the entire budget thinking and returned zero
# characters, and `google/gemini-3.8-flash` truncated mid-JSON. Both were caught and fell back
# to the baseline, which is the system working — but a compiler that never compiles is no use.
DEFAULT_MODEL = "google/gemini-3.7-flash"

# OpenRouter routes to the next of these if the primary is down or rate-limited. Free
# redundancy for the "predictable when optional models fail" requirement — a provider outage
# degrades to another model before it degrades to the regex baseline. Ordered by measured
# accuracy, since a fallback that compiles the wrong policy is worse than a slow one. All
# three support the structured-output mode below; check that before adding one.
DEFAULT_FALLBACK_MODELS = ("anthropic/claude-opus-5", "google/gemini-3.5-flash-lite")

# Generous on purpose. There is no platform deadline anywhere near this call — the 8-second
# budget belongs to the decision path, which never reaches this module.
DEFAULT_TIMEOUT_S = 60.0

# Swisscom's hosted Apertus, the Swiss open model (Swiss {ai} Weeks hacker guide, 2026-09-24).
# OpenAI-compatible, one model per endpoint, and a Bearer key from keymaker.ai-weeks.ch that
# EXPIRES AFTER 60 MINUTES: an expired key is an HTTP 401, which degrades to the baseline like
# any other, so a compile that suddenly reads `model_error: http_401` wants a fresh key, not a
# bug hunt. Quota: 5 requests/s, 10 M input and 2.5 M output tokens for the event.
APERTUS_BASE_URL = "https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1"
APERTUS_MODEL = "swiss-ai/Apertus-v1.5-70B"


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible endpoint, and what it does and does not support.

    The request, the guard and the fallback are the same for every provider; this records only
    where they genuinely differ, so adding one is a table row rather than a second code path.
    """

    name: str
    base_url: str
    key_env: str
    model_env: str
    default_model: str
    # Only OpenRouter accepts a `models` array and routes past a dead primary. Elsewhere there
    # is one model, and its failure goes straight to the baseline.
    routing: bool
    # OpenAI-style structured outputs show the schema, descriptions included, to the model. A
    # self-hosted server enforcing it by grammar constrains the tokens but never shows it, so
    # the model would not see "provenance must be verbatim" at all. There the schema goes into
    # the system prompt as well.
    schema_in_prompt: bool
    # Send the schema as `response_format` for the server to enforce. Off where enforcement
    # breaks the output rather than shaping it — see the Apertus row.
    structured_output: bool
    max_tokens: int
    base_url_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def url(self) -> str:
        override = os.environ.get(self.base_url_env, "").strip() if self.base_url_env else ""
        return override or self.base_url


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider(
        name="openrouter",
        base_url=BASE_URL,
        key_env="OPENROUTER_API_KEY",
        model_env="LEASH_COMPILER_MODEL",
        default_model=DEFAULT_MODEL,
        routing=True,
        schema_in_prompt=False,
        structured_output=True,
        # The IR itself is ~1.5 k tokens, but on a reasoning model this budget covers the
        # thinking too, and thinking is unbounded. Generous because it costs nothing unused:
        # OpenRouter bills tokens generated, not tokens allowed.
        max_tokens=32000,
        headers={
            # OpenRouter uses these for its public rankings. Ours, not the cardholder's:
            # nothing about a customer or a purchase is ever sent in a header.
            "HTTP-Referer": "https://github.com/saw26-wallet-control-layer",
            "X-Title": "Wallet Control Layer (Viseca, Swiss {ai} Weeks 2026)",
        },
    ),
    "apertus": Provider(
        name="apertus",
        base_url=APERTUS_BASE_URL,
        base_url_env="APERTUS_BASE_URL",
        key_env="SWISSCOM_API_KEY",
        model_env="APERTUS_MODEL",
        default_model=APERTUS_MODEL,
        routing=False,
        schema_in_prompt=True,
        # Measured 2026-09-24: with the strict schema as `response_format`, Apertus opened the
        # JSON correctly and then emitted whitespace until max_tokens — 4 000 tokens, 66 s, not
        # parseable. Grammar-constrained decoding permits unlimited whitespace and the model
        # took it. The same request with the schema in the prompt only finished in 13 s at 743
        # tokens. The guard re-checks every field either way, so nothing is enforced unchecked.
        structured_output=False,
        # Three limits, all measured 2026-09-24. The gateway (a) reserves max_tokens against a
        # 12 500 output-tokens-per-minute budget up front, so 16 000 is refused with a 429 every
        # time and every token asked for is a token of throughput spent; (b) times out at ~55 s
        # with a 504; (c) Apertus generates ~62 tokens/s. So nothing past ~3 400 tokens can
        # arrive anyway, and an IR is ~750. 3 000 leaves 4x headroom and ~4 compiles a minute.
        max_tokens=3000,
    ),
}
DEFAULT_PROVIDER = "openrouter"


def provider() -> Provider:
    """The provider `LEASH_COMPILER_PROVIDER` names. Unknown or unset is OpenRouter."""
    name = (os.environ.get("LEASH_COMPILER_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if name not in PROVIDERS:
        log.warning("unknown LEASH_COMPILER_PROVIDER %r, using %s", name, DEFAULT_PROVIDER)
        name = DEFAULT_PROVIDER
    return PROVIDERS[name]


# The IR shape the model may return. Enforced twice: by the provider through structured
# outputs, and again by contract.accept(), which is the one that actually decides. Provenance is
# `required` on every item so "quote the customer's words" is a schema property, not a hope.
_PROVENANCE = {
    "type": "string",
    "description": (
        "The exact span of the customer's instruction this came from, copied verbatim. "
        "Anything that is not a literal substring of the instruction is discarded."
    ),
}
_CONFIDENCE = {
    "type": "string",
    "enum": ["high", "medium", "low"],
    "description": (
        "high = the instruction states this outright. medium = it clearly implies it but a "
        "judgement was needed. low = you are guessing; the customer will be asked instead."
    ),
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "uncertainty_policy": {
            "type": "string",
            "enum": ["ask", "decline", "approve"],
            "description": "What to do when a check cannot be answered. Default 'ask'.",
        },
        "rules": {
            "type": "array",
            "description": "Spend caps only. Everything else is an intent facet.",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": ["billing_amount_chf"]},
                    "operator": {"type": "string", "enum": ["<=", "<"]},
                    "value": {"type": "number", "description": "The cap, in CHF."},
                    "scope": {
                        "type": "string",
                        "enum": ["purchase", "period"],
                        "description": (
                            "'purchase' caps one order. 'period' caps the total across a "
                            "rolling window, and then period_days is required."
                        ),
                    },
                    "period_days": {"type": ["integer", "null"]},
                    "confidence": _CONFIDENCE,
                    "provenance": _PROVENANCE,
                },
                "required": [
                    "field",
                    "operator",
                    "value",
                    "scope",
                    "period_days",
                    "confidence",
                    "provenance",
                ],
                "additionalProperties": False,
            },
        },
        "intent_facets": {
            "type": "array",
            "description": "Requirements a spend cap cannot express.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(FACET_REQUIRE_KEYS)},
                    "require": {
                        "type": "object",
                        "properties": {
                            "item_category_in": {
                                "type": ["array", "null"],
                                "items": {"type": "string", "enum": sorted(ITEM_CATEGORIES)},
                            },
                            "item_keywords_all": {
                                "type": ["array", "null"],
                                "items": {"type": "string"},
                                "description": (
                                    "Lowercase words that must ALL appear in the product's "
                                    "name for it to be the thing the customer asked for. "
                                    "Distinguishing words only — 'road', 'running', '27'. "
                                    "Never a word every product in the category would have."
                                ),
                            },
                            "item_description": {
                                "type": ["string", "null"],
                                "description": "The requested product, in the customer's words.",
                            },
                            "size": {"type": ["integer", "null"]},
                            "prior_approvals_min": {
                                "type": ["integer", "null"],
                                "description": "Previous approved purchases required at a shop.",
                            },
                            "merchant_category_in": {
                                "type": ["array", "null"],
                                "items": {"type": "string", "enum": sorted(MERCHANT_CATEGORIES)},
                            },
                            "return_window_days_min": {"type": ["integer", "null"]},
                            "item_category_not_in": {
                                "type": ["array", "null"],
                                "items": {"type": "string", "enum": sorted(ITEM_CATEGORIES)},
                                "description": "Kinds of product the customer ruled out.",
                            },
                            "merchant_category_not_in": {
                                "type": ["array", "null"],
                                "items": {"type": "string", "enum": sorted(MERCHANT_CATEGORIES)},
                                "description": "Kinds of shop the customer ruled out.",
                            },
                            "item_keywords_none": {
                                "type": ["array", "null"],
                                "items": {"type": "string"},
                                "description": (
                                    "A product the customer ruled out that is not a category "
                                    "of its own, as the lowercase words products are NAMED "
                                    "with: 'no alcohol' is ['wine', 'spirits', 'beer']. Never "
                                    "a word ordinary products share, like 'drinks' or 'and'."
                                ),
                            },
                            "hours_from": {
                                "type": ["integer", "null"],
                                "description": "First hour of day (0-23, UTC) buying is allowed.",
                            },
                            "hours_to": {
                                "type": ["integer", "null"],
                                "description": "Hour of day (0-23, UTC) buying stops, exclusive.",
                            },
                            "weekdays": {
                                "type": ["array", "null"],
                                "items": {
                                    "type": "string",
                                    "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                },
                                "description": (
                                    "The days buying is allowed, in the customer's own time. "
                                    "'never at the weekend' is mon to fri."
                                ),
                            },
                            "cancellable": {
                                "type": ["boolean", "null"],
                                "description": (
                                    "true when only orders that can be cancelled for a refund "
                                    "are allowed: 'refundable rate only', 'free cancellation'. "
                                    "Never true for 'non-refundable'. Otherwise null."
                                ),
                            },
                        },
                        "required": [
                            "item_category_in",
                            "item_keywords_all",
                            "item_description",
                            "size",
                            "prior_approvals_min",
                            "merchant_category_in",
                            "return_window_days_min",
                            "item_category_not_in",
                            "merchant_category_not_in",
                            "item_keywords_none",
                            "hours_from",
                            "hours_to",
                            "weekdays",
                            "cancellable",
                        ],
                        "additionalProperties": False,
                    },
                    "confidence": _CONFIDENCE,
                    "provenance": _PROVENANCE,
                    "open_question": {
                        "type": ["string", "null"],
                        "description": (
                            "If confidence is low, the question to put to the customer instead "
                            "of enforcing this. Plain language, no jargon."
                        ),
                    },
                },
                "required": ["kind", "require", "confidence", "provenance", "open_question"],
                "additionalProperties": False,
            },
        },
        "guidance": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One plain sentence per rule, addressed to the customer, saying what will "
                "happen. 'Each order stays at or below CHF 120.' No reason codes, no jargon."
            ),
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Anything genuinely ambiguous in the instruction, as a question the customer "
                "can answer. Do not invent doubt about things the instruction states plainly."
            ),
        },
    },
    "required": ["uncertainty_policy", "rules", "intent_facets", "guidance", "open_questions"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You compile a bank customer's own shopping instruction into machine-checkable permissions for
an AI shopping agent. Your output is reviewed by that customer before it takes effect, and
then enforced by deterministic code that never calls you again.

Two failure modes, both real:
- Too permissive: the agent spends money on something the customer did not authorise.
- Too restrictive: the agent is blocked from a purchase the customer plainly wanted. This is
  just as bad. Do not invent requirements the instruction does not contain.

Rules you must follow:

1. PROVENANCE. Every rule and facet quotes the exact span of the instruction it came from,
   copied character for character. Anything that is not a literal substring of the customer's
   text is discarded, so paraphrase costs you the rule.
2. CONFIDENCE IS LOAD-BEARING. `high` = stated outright. `medium` = clearly implied, some
   judgement. `low` = you are guessing. A low-confidence facet is NOT enforced — it becomes a
   question for the customer, which is the right outcome when you are unsure. Never mark a
   guess `high` to make it stick.
3. VOCABULARY IS CLOSED. Categories must come from the lists in the schema. If the customer
   asks for a kind of shop or product that is not in them, do not pick the nearest one — leave
   it out and write an open question instead.
4. SPEND CAPS ARE `rules`. Everything else is an `intent_facet`. A cap with `scope: period`
   must state `period_days`. A price PER UNIT ("CHF 200 per night") is not an order cap: when
   the instruction also says how many units ("3 nights"), the per-order cap is their product
   (CHF 600), quoting the price as provenance. Without a count, cap each order at the unit
   price and ask in `open_questions` how many units one order may cover.
5. KEYWORDS DISTINGUISH, THEY DO NOT DESCRIBE. `item_keywords_all` must all appear in a
   product's catalogue name. "road" and "running" are good for road-running shoes; "shoes" is
   already covered by the category, and a word like "household" or "worn" appears in no
   product name and would block every purchase. A place ("a hotel in Munich") is never a
   keyword: it is where the shop is, no product is named after it, and where a shop is
   cannot be checked yet, so say so in `open_questions`. Emit at most ONE facet of each
   kind: put every exclusion in the same category_exclusion.
6. GUIDANCE IS FOR A PERSON, AND IT COVERS EVERYTHING. Short sentences, second person, no
   field names. Write one line for EVERY rule and EVERY facet you emit, so the customer
   reads a complete account of what will happen before they consent. A cap explained and
   four requirements left unmentioned is a review screen that under-reports what the agent
   will actually enforce. One sentence may cover two closely related requirements, but
   nothing may go unmentioned.
7. DO NOT INVENT DEGREE. Provenance constrains WHETHER a requirement exists; it does not
   constrain how strict you make it, so that part is on you. Where a requirement takes a
   number or a set, use the value the words justify and no other.
   - `prior_approvals_min` is 1 unless the customer names a count. "a shop I use regularly"
     requires that they have shopped there, not a threshold you choose; picking 2 declines a
     shop they have used once, which they did not ask for.
   - For a category list, pick the SMALLEST set that covers what was asked. Road-running
     shoes are `sporting_goods`; adding `clothing` widens the permission rather than
     describing it. Add a second category only when the request genuinely spans both.
   Stricter than the words is over-blocking. Broader than the words is under-blocking. Both
   are wrong, and neither is the safe default.

The facets you may emit, and what each one checks:
- item_identity — is the thing the customer asked for actually in the basket?
- item_attribute — is it the right variant? (size only)
- merchant_familiarity — has the customer bought from this shop before?
- merchant_type — is this the kind of shop the customer named?
- order_terms — can the order be returned, and for long enough? Is it refundable?
  "refundable rate only" or "free cancellation" is `cancellable: true`; "non-refundable" never is.
- no_additions — is there anything in the basket beyond what was asked for?
- category_exclusion — does the basket contain something the customer ruled out? Write every
  exclusion the customer states ("no gift cards", "no cosmetics", "no alcohol") as one
  category_exclusion, EVEN WHEN an allow-list already implies it: the customer should read
  their own words on the review screen, and a mixed basket that carries one excluded line is
  only declined by the exclusion. A kind of product goes in `item_category_not_in`. A product
  that is not a category of its own (alcohol is sold as groceries) goes in
  `item_keywords_none`, as the words products are named with.

- spending_hours — is the order placed on a day and at an hour the customer allows? Days the
  customer excludes ("never at the weekend", "weekdays only") go in `weekdays`
  (mon to fri). Hours only when the customer states clock times ("between 8 and 20"). A meal
  ("dinners", "lunch") is not a stated hour: if you think it implies hours, emit them at
  confidence low so the customer is asked, never as a rule.

If the instruction expresses a requirement none of these can check, do not force it into the
nearest one. Say so in `open_questions`. A count of orders ("one delivery a day") is such a
requirement today.

Reply with the JSON object only.
"""


def api_key() -> str:
    return os.environ.get(provider().key_env, "").strip()


def unavailable_note() -> str:
    """The compiler note for "no key", naming the variable the active provider reads."""
    return f"model_unavailable: {provider().key_env} is not set"


def models() -> list[str]:
    """The primary model first, then OpenRouter's routing fallbacks where there are any."""
    active = provider()
    primary = os.environ.get(active.model_env, "").strip() or active.default_model
    if not active.routing:
        return [primary]
    configured = os.environ.get("LEASH_COMPILER_FALLBACK_MODELS")
    rest = (
        [m.strip() for m in configured.split(",") if m.strip()]
        if configured is not None
        else list(DEFAULT_FALLBACK_MODELS)
    )
    return [primary, *(m for m in rest if m != primary)]


def _client(timeout: float) -> httpx.Client:
    """The HTTP client for the active provider. Separated so the tests can stand in for it."""
    active = provider()
    return httpx.Client(
        base_url=active.url(),
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            **active.headers,
        },
    )


def output_schema() -> dict[str, Any]:
    """`OUTPUT_SCHEMA` with its category enums set to the vocabulary in force.

    Refreshed in place, not rebuilt: the live pack registers categories the repo pack never
    used (`photography`, `travel`), and a model constrained to the import-time enum could not
    name the category the SCEN0122 camera lens is filed under (domain/settings.py).
    """
    facet_require = OUTPUT_SCHEMA["properties"]["intent_facets"]["items"]["properties"]["require"]
    for key, props in facet_require["properties"].items():
        enum = props.get("items", {}).get("enum") if isinstance(props, dict) else None
        if enum is not None and key in category_vocabulary():
            props["items"]["enum"] = sorted(category_vocabulary()[key])
    return OUTPUT_SCHEMA


def system_prompt() -> str:
    """The instructions, plus the schema itself where the provider will not show it."""
    if not provider().schema_in_prompt:
        return SYSTEM_PROMPT
    # Strict schemas must list every `require` key as required, so a model reading the schema
    # rather than being constrained by it fills every key in every facet. Say which ones count.
    reads = "\n".join(
        f"- {kind}: {', '.join(sorted(keys)) or 'none — its presence is the requirement'}"
        for kind, keys in sorted(FACET_REQUIRE_KEYS.items())
    )
    return (
        f"{SYSTEM_PROMPT}\n"
        "Your reply must be one JSON object that validates against this JSON Schema. The "
        "`description` fields are part of your instructions:\n\n"
        f"{json.dumps(output_schema(), indent=1)}\n\n"
        "In each facet's `require`, fill only the keys that kind reads and set every other key "
        "to null. A value under a key its kind does not read is discarded:\n"
        f"{reads}\n"
    )


def request_body(instruction: str) -> dict[str, Any]:
    """The POST body. Pure, so a test can assert its shape without a network."""
    chosen = models()
    body: dict[str, Any] = {
        "model": chosen[0],
        # A compiler should compile the same text the same way. OpenRouter passes this
        # through to whichever provider serves the request.
        "temperature": 0,
        "max_tokens": provider().max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {
                "role": "user",
                "content": (
                    "Compile this customer instruction.\n\n"
                    f"<instruction>\n{instruction}\n</instruction>"
                ),
            },
        ],
    }
    if provider().structured_output:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "policy_ir", "strict": True, "schema": output_schema()},
        }
    if len(chosen) > 1:
        body["models"] = chosen
    return body


@functools.cache
def catalogue() -> dict[str, list[str]] | None:
    """The product names the guard checks keywords against (rail 5b), or None without a pack.

    None is safe, not silent: rail 5b needs the catalogue to prove a keyword matches nothing,
    so without it the keyword is kept, exactly as before the rail existed.
    """
    try:
        return item_catalogue()
    except (OSError, KeyError) as exc:
        log.warning("no item catalogue, keyword rail 5b is off: %s: %s", type(exc).__name__, exc)
        return None


@functools.cache
def places() -> frozenset[str] | None:
    """The words the pack's shop cities are written with (rail 5d), or None without a pack."""
    try:
        return merchant_places()
    except (OSError, KeyError) as exc:
        log.warning("no merchant cities, keyword rail 5d is off: %s: %s", type(exc).__name__, exc)
        return None


def _json_content(content: str) -> Any:
    """Parse the reply, tolerating one Markdown code fence around it.

    Some servers answer ```json … ``` even when told not to. Unwrapping it is a parsing repair,
    not a policy one: whatever is inside still goes through contract.accept() whole.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rstrip().removesuffix("```")
    return json.loads(text)


def _call_model(instruction: str, *, timeout: float) -> tuple[Any, str | None, str]:
    """Return (parsed JSON, failure note, model that answered). Never raises."""
    active = provider()
    if not api_key():
        return None, unavailable_note(), ""

    try:
        with _client(timeout) as client:
            response = client.post("/chat/completions", json=request_body(instruction))
    # Timeout, connection failure, DNS — every one of them has the same correct response
    # here: use the baseline. A per-class chain would be several branches doing one thing,
    # so we record the class name for the log and degrade (AGENTS.md §4).
    except Exception as exc:
        log.warning("policy compiler fell back to baseline: %s: %s", type(exc).__name__, exc)
        return None, f"model_error: {type(exc).__name__}", ""

    if response.status_code >= 400:
        log.warning("policy compiler: %s returned %s", active.name, response.status_code)
        return None, f"model_error: http_{response.status_code}", ""

    try:
        payload = response.json()
    except ValueError:
        return None, "model_output_invalid: response was not JSON", ""

    # OpenRouter reports some upstream failures as a 200 with an error envelope. Swisscom's
    # gateway sends `error` as a bare string beside a top-level `code`, so read both shapes.
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else payload.get("code") or error
        return None, f"model_error: {active.name}_{code or 'unknown'}", ""

    served = str((payload or {}).get("model") or "")
    choices = (payload or {}).get("choices") or []
    if not choices:
        return None, "model_output_invalid: no choices returned", served

    choice = choices[0]
    if choice.get("finish_reason") == "length":
        # Truncated JSON would fail to parse below anyway; naming it makes the log useful.
        return None, "model_output_invalid: response was truncated", served

    content = (choice.get("message") or {}).get("content")
    if not isinstance(content, str):
        return None, "model_output_invalid: no text content", served
    try:
        return _json_content(content), None, served
    except json.JSONDecodeError:
        return None, "model_output_invalid: content was not JSON", served


def compile_instruction(
    instruction: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Compile with the model, falling back to the baseline on any failure whatsoever.

    The fallback is total: the returned IR is either wholly the model's (guarded, floored) or
    wholly the baseline's. A half-and-half IR would be the one artefact nobody could reason
    about (specs/llm-compiler.md, "Fallback").
    """
    timeout = timeout or float(os.environ.get("LEASH_COMPILER_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    if model:
        os.environ[provider().model_env] = model

    floor = baseline_compiler.compile_instruction(instruction)
    raw, failure, served = _call_model(instruction, timeout=timeout)

    # Which model *answered*, not which we asked for — OpenRouter may have routed past a
    # rate-limited primary, and the audit trail should say what actually compiled the policy.
    ir = (
        None
        if failure
        else accept(
            raw,
            instruction,
            model=served or models()[0],
            catalogue=catalogue(),
            places=places(),
        )
    )
    if ir is None:
        return fallback(floor, failure or "model_output_empty: nothing survived the guard")

    return apply_safety_floor(ir, floor)


def fallback(baseline_ir: dict[str, Any], reason: str) -> dict[str, Any]:
    """The baseline IR, labelled with why the model did not supply one."""
    from .contract import instruction_hash

    return {
        **baseline_ir,
        "instruction_sha256": instruction_hash(baseline_ir["source_instruction"]),
        "compiler_notes": [reason, "fell_back_to_baseline"],
    }
