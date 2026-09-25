"""The standing preferences layer — the customer, rather than the errand.

specs/customer-settings.md §2. A mandate is an errand and dies with it. This layer is the
person: it survives every mandate, seeds the next one, and is the half of the brief's
*"customer-managed wallet policy"* we did not have.

The document is deliberately flat — one entry per key in the settings catalogue — because it
is edited by a person in a phone screen, not compiled from prose:

```json
{
  "per_order_limit_chf": 300,
  "uncertainty_policy": "ask",
  "merchant_familiarity": {"prior_approvals_min": 1},
  "merchant_type": {"merchant_category_in": ["groceries", "household"]},
  "order_terms": {"return_window_days_min": 14},
  "no_additions": true,
  "origins": {"no_additions": {"source": "profile", "quote": "avoids gift vouchers"}}
}
```

`origins` is how a setting the customer *accepted from their profile* keeps saying so on the
review screen (specs/customer-settings.md §5 and §8). Everything else defaults to the
`preferences` source.

Validation is strict and says why, because every refusal here is a sentence the UI shows:
a key nothing reads would render as an enforced rule and enforce nothing, and a period key
would be a loosening wearing a tightening's clothes (§3.4).

Pure. No I/O, no clock.
"""

from __future__ import annotations

from typing import Any

from . import provenance as prov
from .compose import Layer
from .settings import (
    BY_KEY,
    INSTRUCTION_ONLY_KEYS,
    STANDING_KEYS,
    STEP_UP_SENSITIVITY,
    UNCERTAINTY_POLICIES,
    category_vocabulary,
)

EMPTY: dict[str, Any] = {}


class PreferenceError(ValueError):
    """One refused setting, with the code the service turns into an error envelope."""

    def __init__(self, code: str, setting: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.setting = setting
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "setting": self.setting, "message": self.message}


def validate(body: dict[str, Any]) -> tuple[dict[str, Any], list[PreferenceError]]:
    """Clean a submitted preferences document. Returns what survives and every refusal.

    Never raises: the caller needs the whole list so the UI can mark every offending field at
    once rather than one per round trip.
    """
    clean: dict[str, Any] = {}
    errors: list[PreferenceError] = []
    origins = body.get("origins") if isinstance(body.get("origins"), dict) else {}

    for key, value in body.items():
        if key == "origins":
            continue
        setting = BY_KEY.get(key)
        if setting is None:
            errors.append(
                PreferenceError(
                    "unknown_setting", key, f"no check reads {key!r}, so it cannot be enforced"
                )
            )
            continue
        if key not in STANDING_KEYS:
            errors.append(
                PreferenceError(
                    "setting_not_standing",
                    key,
                    f"{setting.label.lower()} belongs to one errand, not to you — "
                    "set it on the mandate instead",
                )
            )
            continue
        if value is None:
            continue  # explicit clear
        try:
            clean[key] = _coerce(key, value)
        except PreferenceError as exc:
            errors.append(exc)

    kept = {k: _origin(v) for k, v in (origins or {}).items() if k in clean}
    if kept:
        clean["origins"] = kept
    return clean, errors


def _origin(raw: Any) -> dict[str, str]:
    entries = prov.normalise(raw)
    return dict(entries[0]) if entries else {"source": "preferences", "quote": ""}


def _coerce(key: str, value: Any) -> Any:
    if key == "per_order_limit_chf":
        amount = _positive_number(key, value)
        return amount
    if key == "period_limit_chf":
        # `period_days` is mandatory and positive. A period rule without a window cannot be
        # enforced — it reaches `check_period_limit` as `unknown` — so accepting one here
        # would store a ceiling that quietly turns every order into a step-up.
        body = value if isinstance(value, dict) else {"value": value}
        days = body.get("period_days")
        if days is None:
            raise PreferenceError(
                "invalid_value", key, "say how many days the limit covers, e.g. 30"
            )
        return {
            "value": _positive_number(key, body.get("value")),
            "period_days": _positive_int(key, days),
        }
    if key == "uncertainty_policy":
        text = str(value)
        if text not in UNCERTAINTY_POLICIES:
            raise PreferenceError(
                "invalid_value", key, f"{text!r} is not one of {sorted(UNCERTAINTY_POLICIES)}"
            )
        return text
    if key == "no_additions":
        return bool(value)
    if key == "step_up_threshold":
        # Stored as the word the customer chose, not the number. The three positions are
        # measured (`make tune-sweep`); offering a free number would imply a precision the
        # board does not have, and would let a preferences document carry a value nobody
        # could explain.
        text = str(value)
        if text not in STEP_UP_SENSITIVITY:
            raise PreferenceError(
                "invalid_value", key, f"{text!r} is not one of {sorted(STEP_UP_SENSITIVITY)}"
            )
        return text
    if key == "spending_hours":
        body = value if isinstance(value, dict) else {}
        start = _hour_of_day(key, body.get("hours_from"))
        end = _hour_of_day(key, body.get("hours_to"))
        if start == end:
            raise PreferenceError(
                "invalid_value",
                key,
                "the start and end hour are the same, which could mean no hours or every hour",
            )
        return {"hours_from": start, "hours_to": end}
    if key == "category_exclusion":
        body = value if isinstance(value, dict) else {}
        out: dict[str, Any] = {}
        for field, vocabulary in (
            ("item_category_not_in", category_vocabulary()["item_category_not_in"]),
            ("merchant_category_not_in", category_vocabulary()["merchant_category_not_in"]),
        ):
            raw = body.get(field) or []
            values = [str(v) for v in raw] if isinstance(raw, (list, tuple)) else []
            unknown = [v for v in values if v not in vocabulary]
            if unknown:
                raise PreferenceError(
                    "unknown_category", key, f"not a kind we can check: {', '.join(unknown)}"
                )
            if values:
                out[field] = sorted(set(values))
        if not out:
            raise PreferenceError(
                "invalid_value", key, "name at least one kind to exclude, or clear the setting"
            )
        return out
    if key == "merchant_familiarity":
        return {
            "prior_approvals_min": _non_negative_int(key, _unwrap(value, "prior_approvals_min"))
        }
    if key == "order_terms":
        return {
            "return_window_days_min": _non_negative_int(
                key, _unwrap(value, "return_window_days_min")
            )
        }
    if key == "merchant_type":
        raw = _unwrap(value, "merchant_category_in")
        values = [str(v) for v in raw] if isinstance(raw, (list, tuple)) else []
        vocabulary = category_vocabulary()["merchant_category_in"]
        unknown = [v for v in values if v not in vocabulary]
        if unknown:
            raise PreferenceError(
                "unknown_category", key, f"not a kind of shop we can check: {', '.join(unknown)}"
            )
        if not values:
            raise PreferenceError(
                "invalid_value", key, "name at least one kind of shop, or clear the setting"
            )
        return {"merchant_category_in": sorted(set(values))}
    raise PreferenceError("unknown_setting", key, f"{key!r} has no handler")


def _unwrap(value: Any, key: str) -> Any:
    """Accept both the bare value and the `require`-shaped object the IR uses."""
    if isinstance(value, dict):
        return value.get(key, value.get("value"))
    return value


def _positive_number(key: str, value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise PreferenceError("invalid_value", key, f"{value!r} is not an amount") from exc
    if amount <= 0:
        raise PreferenceError("invalid_value", key, "an amount must be above zero")
    return amount


def _as_float(value: Any) -> float:
    """Money that has already been validated, read back without re-raising.

    `validate()` is the gate; by the time a value reaches the renderers and `apply_settings`
    it is a number. A malformed one here would be a bug upstream, and answering `0.0` keeps
    the pure layer free of raises (AGENTS.md §3.5) while making the bug visible.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _hour_of_day(key: str, value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise PreferenceError("invalid_value", key, "say which hour, 0 to 23")
    try:
        hour = int(value)
    except (TypeError, ValueError) as exc:
        raise PreferenceError("invalid_value", key, f"{value!r} is not an hour") from exc
    if not 0 <= hour <= 23:
        raise PreferenceError("invalid_value", key, "an hour runs from 0 to 23")
    return hour


def _positive_int(key: str, value: Any) -> int:
    number = _non_negative_int(key, value)
    if number <= 0:
        raise PreferenceError("invalid_value", key, "this must be at least one day")
    return number


def _non_negative_int(key: str, value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PreferenceError("invalid_value", key, f"{value!r} is not a whole number") from exc
    if number < 0:
        raise PreferenceError("invalid_value", key, "this cannot be negative")
    return number


# ------------------------------------------------------------------ to policy


def to_layer(prefs: dict[str, Any]) -> Layer:
    """The standing preferences as a composable layer, with every origin recorded."""
    origins = prefs.get("origins") or {}
    rules: list[dict[str, Any]] = []
    facets: list[dict[str, Any]] = []

    def carry(key: str, body: dict[str, Any]) -> dict[str, Any]:
        """Stamp one rule or facet with the layer that contributed it.

        A preference the customer set themselves writes no quote: the mandate row already
        states the rule ("Only electronics shops"), so repeating it underneath as its own
        provenance says the same thing twice. The *source* is the origin worth naming, and
        `provenance.provText` falls back to "from your preferences". A preference accepted
        from the profile keeps its own quote, because there the origin really is a different
        sentence — the profile field it came from.
        """
        origin = origins.get(key) or {}
        return {
            **body,
            "provenance": [
                {
                    "source": str(origin.get("source") or "preferences"),
                    "quote": str(origin.get("quote") or ""),
                }
            ],
            "confidence": "high",
        }

    if (cap := prefs.get("per_order_limit_chf")) is not None:
        rules.append(
            carry(
                "per_order_limit_chf",
                {
                    "field": "billing_amount_chf",
                    "operator": "<=",
                    "value": cap,
                    "currency": "CHF",
                    "scope": "purchase",
                },
            )
        )
    # Layerable since multi-window enrichment: a standing monthly ceiling and an errand's
    # weekly one are two constraints, not a contest one of them loses (decision-rules.md §9).
    if (period := prefs.get("period_limit_chf")) is not None:
        rules.append(
            carry(
                "period_limit_chf",
                {
                    "field": "billing_amount_chf",
                    "operator": "<=",
                    "value": period["value"],
                    "currency": "CHF",
                    "scope": "period",
                    "period_days": period["period_days"],
                },
            )
        )
    for key in (
        "merchant_familiarity",
        "merchant_type",
        "order_terms",
        "category_exclusion",
        "spending_hours",
    ):
        if (require := prefs.get(key)) is not None:
            facets.append(carry(key, {"kind": key, "require": dict(require)}))
    if prefs.get("no_additions"):
        facets.append(carry("no_additions", {"kind": "no_additions"}))

    return Layer(
        source="preferences",
        hard_rules=tuple(rules),
        intent_facets=tuple(facets),
        uncertainty_policy=prefs.get("uncertainty_policy"),
        step_up_threshold=STEP_UP_SENSITIVITY.get(str(prefs.get("step_up_threshold", ""))),
    )


def label_for(key: str, prefs: dict[str, Any]) -> str:
    """The sentence the customer sees beside a preference-sourced rule."""
    value = prefs.get(key)
    if key == "per_order_limit_chf":
        return f"Never more than CHF {_as_float(value):.2f} on one order"
    if key == "period_limit_chf":
        body = value or {}
        return (
            f"Never more than CHF {_as_float(body.get('value')):.2f} "
            f"across any {int(body.get('period_days', 0))} days"
        )
    if key == "merchant_familiarity":
        minimum = int((value or {}).get("prior_approvals_min", 1))
        return (
            "Only shops I have used before"
            if minimum <= 1
            else f"Only shops I have used {minimum}+ times"
        )
    if key == "merchant_type":
        kinds = ", ".join((value or {}).get("merchant_category_in", [])).replace("_", " ")
        return f"Only {kinds} shops"
    if key == "order_terms":
        return f"Returnable within {int((value or {}).get('return_window_days_min', 0))}+ days"
    if key == "no_additions":
        return "Nothing added I did not ask for"
    if key == "step_up_threshold":
        return {
            "more": "Ask me at the first sign of trouble",
            "normally": "Ask me when something looks wrong",
            "less": "Only ask me when it is serious",
        }.get(str(value), f"Ask me: {value}")
    if key == "spending_hours":
        body = value or {}
        return (
            f"Only between {int(body.get('hours_from', 0)):02d}:00 "
            f"and {int(body.get('hours_to', 0)):02d}:00"
        )
    if key == "category_exclusion":
        body = value or {}
        parts = []
        if items := body.get("item_category_not_in"):
            parts.append("never " + ", ".join(items).replace("_", " "))
        if shops := body.get("merchant_category_not_in"):
            parts.append("never from " + ", ".join(shops).replace("_", " ") + " shops")
        return "; ".join(parts) or "Nothing excluded"
    if key == "uncertainty_policy":
        return "Ask me when unclear" if value == "ask" else f"When unclear: {value}"
    return BY_KEY[key].label if key in BY_KEY else key


def apply_settings(ir: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """A Policy IR with these settings applied, **semantically** — the policy as meant.

    Replacement, not addition: a customer who sets the per-order limit to 250 means 250, and
    `classify` needs to see that to tell a narrowing from a widening. The add-only list the
    platform's `PATCH` requires is rebuilt afterwards by the caller, and only when the
    amendment turned out to be a tightening — see `Supervisor.amend_mandate`.
    """
    rules = [dict(r) for r in ir.get("rules") or ir.get("hard_rules") or ()]
    facets = [dict(f) for f in ir.get("intent_facets") or ()]
    out = {**ir, "rules": rules, "intent_facets": facets}

    for key, value in settings.items():
        if key == "origins":
            continue
        if key == "per_order_limit_chf":
            rules = [r for r in rules if r.get("scope") != "purchase"]
            if value is not None:
                rules.append(
                    prov.stamp(
                        {
                            "field": "billing_amount_chf",
                            "operator": "<=",
                            "value": _as_float(value),
                            "currency": "CHF",
                            "scope": "purchase",
                        },
                        "amendment",
                        f"per-order limit set to CHF {float(value):.2f} in the app",
                    )
                )
            out["rules"] = rules
        elif key == "period_limit_chf":
            rules = [r for r in rules if r.get("scope") != "period"]
            if value is not None:
                amount = _as_float(value if not isinstance(value, dict) else value.get("value"))
                days = value.get("period_days") if isinstance(value, dict) else None
                rules.append(
                    prov.stamp(
                        {
                            "field": "billing_amount_chf",
                            "operator": "<=",
                            "value": amount,
                            "currency": "CHF",
                            "scope": "period",
                            # Mandatory and validated: a period rule without a window wins the
                            # shortest-window contest and disables period enforcement
                            # outright. specs/customer-settings.md §3.4.
                            "period_days": int(days) if days else 0,
                        },
                        "amendment",
                        "period limit set in the app",
                    )
                )
            out["rules"] = rules
        elif key == "uncertainty_policy":
            out["uncertainty_policy"] = str(value)
        elif key == "step_up_threshold":
            out["step_up_threshold"] = STEP_UP_SENSITIVITY.get(str(value))
        elif key in (
            "merchant_familiarity",
            "merchant_type",
            "order_terms",
            "item_attribute",
            "category_exclusion",
            "spending_hours",
        ):
            # The settings control edits the keys it shows. A key only an instruction sets, such
            # as "refundable rate only" beside a return window, is kept: dropping it would widen
            # the mandate behind an edit that never mentioned it (INSTRUCTION_ONLY_KEYS).
            existing = next((f for f in facets if f.get("kind") == key), None)
            instruction_only = INSTRUCTION_ONLY_KEYS.get(key, frozenset())
            kept = {
                k: v
                for k, v in ((existing or {}).get("require") or {}).items()
                if k in instruction_only
            }
            facets = [f for f in facets if f.get("kind") != key]
            if value is not None:
                facets.append(
                    prov.stamp(
                        {"kind": key, "require": {**kept, **dict(value)}, "confidence": "high"},
                        "amendment",
                        f"{BY_KEY[key].label.lower()} set in the app",
                    )
                )
            elif existing is not None and kept:
                # Clearing the setting clears what the setting controls, nothing more.
                facets.append({**existing, "require": kept})
            out["intent_facets"] = facets
        elif key == "no_additions":
            facets = [f for f in facets if f.get("kind") != "no_additions"]
            if value:
                facets.append(
                    prov.stamp(
                        {"kind": "no_additions", "confidence": "high"},
                        "amendment",
                        "no-additions rule set in the app",
                    )
                )
            out["intent_facets"] = facets

    out["hard_rules"] = out["rules"]
    return out


def describe(prefs: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows for the preferences screen: what is set, and where each one came from."""
    origins = prefs.get("origins") or {}
    rows: list[dict[str, Any]] = []
    for key in (k for k in BY_KEY if k in STANDING_KEYS):
        if key not in prefs:
            continue
        origin = origins.get(key) or {"source": "preferences", "quote": ""}
        rows.append(
            {
                "setting": key,
                "label": BY_KEY[key].label,
                "value": prefs[key],
                "sentence": label_for(key, prefs),
                "source": str(origin.get("source") or "preferences"),
                "quote": str(origin.get("quote") or ""),
            }
        )
    return rows
