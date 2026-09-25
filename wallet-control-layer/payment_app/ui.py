"""The LEASH screens, server-rendered.

The cardholder's app as the storyboard draws it: a splash, the sign-in, then — when an agent
asks to connect — the LEASH onboarding: *Leash protects your payments*, five steps, *Confirm
Leash*. That last tap is the OAuth consent. After it, the phone is the Cockpit
(``static/cockpit.js``). The rules that decide still come from the mandate the agent proposes
from the cardholder's instruction in the chat; the onboarding shows the cardholder their own
pattern and what Leash would suggest, and keeps their choices with the grant
(specs/connector.md §7).

Everything here is markup. Colour, type and shape live in ``static/leash.css``; the marks in
``brand.py``. Icons are the storyboard's line icons, drawn once below and handed to the
cockpit script through ``window.WALLET.icons`` so there is one set.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .brand import signet, wordmark

if TYPE_CHECKING:
    from .server import Persona


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


# ------------------------------------------------------------------ icons


def _icon(paths: str, *, size: int = 20, stroke: float = 1.7) -> str:
    return (
        f'<svg viewBox="0 0 20 20" width="{size}" height="{size}" fill="none" '
        f'stroke="currentColor" stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{paths}</svg>'
    )


ICONS: dict[str, str] = {
    "back": _icon('<path d="M12.5 4.5 7 10l5.5 5.5"/>', stroke=2),
    "chevron": _icon('<path d="M8 5l5 5-5 5"/>', size=14, stroke=2),
    "shop": _icon(
        '<path d="M4 8.2 5.3 4.5h9.4L16 8.2"/><path d="M4 8.2h12v7.3H4z"/><path d="M8.3 15.5v-3.6h3.4v3.6"/>'
    ),
    "clock": _icon('<circle cx="10" cy="10" r="6.5"/><path d="M10 6.6V10l2.4 1.5"/>'),
    "bars": _icon('<path d="M5 16V9M10 16V5M15 16v-6"/>'),
    "lock": _icon(
        '<rect x="5" y="9" width="10" height="7.5" rx="1.8"/><path d="M7.5 9V7a2.5 2.5 0 0 1 5 0v2"/>'
    ),
    "scales": _icon(
        '<path d="M10 4v12M6.5 16h7M4.5 7h11"/><path d="M6 7 4 11.5h4L6 7zM14 7l-2 4.5h4L14 7z"/>'
    ),
    "plus": _icon('<circle cx="10" cy="10" r="6.5"/><path d="M10 7v6M7 10h6"/>'),
    "trend": _icon('<path d="M4 14l4-4 3 3 5-6"/><path d="M13 7h3v3"/>'),
    "calendar": _icon(
        '<rect x="4" y="5.5" width="12" height="10.5" rx="2"/><path d="M4 9h12M7.5 4v3M12.5 4v3"/>'
    ),
    "check": _icon('<path d="M5 10.5l3.2 3L15 7"/>', stroke=2),
    "info": _icon('<circle cx="10" cy="10" r="6.5"/><path d="M10 9v4M10 6.8h.01"/>'),
    "user": _icon('<circle cx="10" cy="7.5" r="3"/><path d="M4.5 16.5a5.5 5.5 0 0 1 11 0"/>'),
    "bag": _icon('<path d="M5 7h10l-.8 9H5.8L5 7z"/><path d="M7.5 7V5.5a2.5 2.5 0 0 1 5 0V7"/>'),
    "shield": _icon(
        '<path d="M10 3l6 2.2v4.6c0 3.6-2.5 6.1-6 7.2-3.5-1.1-6-3.6-6-7.2V5.2L10 3z"/>'
    ),
    "card": _icon('<rect x="3" y="5" width="14" height="10" rx="2"/><path d="M3 8.5h14"/>'),
    "search": _icon('<circle cx="9" cy="9" r="5"/><path d="M12.8 12.8l3.5 3.5"/>'),
    "mail": _icon('<rect x="3" y="5" width="14" height="10" rx="2"/><path d="M3 7l7 4.5L17 7"/>'),
    "receipt": _icon(
        '<path d="M5.5 3.5h9v13l-2.2-1.3-2.3 1.3-2.2-1.3-2.3 1.3v-13z"/><path d="M8 8h4M8 11h4"/>'
    ),
    "book": _icon('<rect x="4.5" y="3.5" width="11" height="13" rx="2"/><path d="M8 8h4M8 11h4"/>'),
    "dots": _icon(
        '<circle cx="7.3" cy="7.3" r="1.9" fill="currentColor" stroke="none"/><circle cx="12.7" cy="7.3" r="1.9" fill="currentColor" stroke="none"/>'
        '<circle cx="7.3" cy="12.7" r="1.9" fill="currentColor" stroke="none"/><circle cx="12.7" cy="12.7" r="1.9" fill="currentColor" stroke="none"/>'
    ),
    "bolt": _icon('<path d="M11 3 5 11h4l-1 6 6-8h-4l1-6z"/>'),
    "wallet": _icon('<rect x="3" y="6" width="14" height="9" rx="2"/><path d="M12 10.5h2"/>'),
    "bang": _icon('<path d="M10 4v7M10 14.5h.01"/>', stroke=2),
    "power": _icon('<path d="M10 4v6"/><path d="M6.2 6.5a5.5 5.5 0 1 0 7.6 0"/>'),
    "tag": _icon('<path d="M4 4h6l6 6-6 6-6-6V4z"/><path d="M7.5 7.5h.01"/>'),
    "doc": _icon('<path d="M5 3.5h6l4 4V16.5H5z"/><path d="M11 3.5v4h4"/>'),
}


# ------------------------------------------------------------------ page chrome


def _document(title: str, body: str, *, root: str, scripts: str = "", warm: bool = False) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="{esc(root)}/static/leash.css"></head>
<body><div class="stage"><div class="phone{" phone--warm" if warm else ""}">{body}</div></div>{scripts}</body></html>"""


def _header(*, back: str = "", brand: bool = True) -> str:
    """The onboarding header: a round back button on the left, the mark in the middle."""
    left = back or '<span aria-hidden="true"></span>'
    mark = (
        f'<div class="hd__brand">{signet(24)}<span>Leash</span></div>' if brand else "<span></span>"
    )
    return f'<header class="hd">{left}{mark}<span aria-hidden="true"></span></header>'


def _back_button(**attrs: str) -> str:
    extra = "".join(f' {k}="{esc(v)}"' for k, v in attrs.items())
    return (
        f'<button class="hd__back" type="button" aria-label="Back"{extra}>{ICONS["back"]}</button>'
    )


def _row(icon: str, title: str, sub: str = "", *, tone: str = "", value: str = "") -> str:
    ico = f'<span class="row__ico{f" row__ico--{tone}" if tone else ""}">{ICONS[icon]}</span>'
    body = f'<span class="row__body"><span class="row__k">{title}</span>'
    if sub:
        body += f'<span class="row__s">{sub}</span>'
    body += "</span>"
    val = f'<span class="row__v">{value}</span>' if value else ""
    return f'<div class="row{" row--top" if sub else ""}">{ico}{body}{val}</div>'


def _kv(key: str, value: str) -> str:
    return f'<div class="kv"><span>{key}</span><span class="kv__v">{value}</span></div>'


def _chf(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return esc(value)
    text = f"{number:,.0f}" if number == int(number) else f"{number:,.2f}"
    return text.replace(",", "’")


def error_page(title: str, message: str) -> str:
    body = (
        _header()
        + f'<section class="card">{_row("lock", esc(title), esc(message), tone="grey")}</section>'
    )
    return _document(title, body, root="")


# ------------------------------------------------------------------ splash · sign in


def splash_page(*, next_url: str, root: str) -> str:
    body = f"""<a class="splash" href="{esc(root)}/login?{urlencode({"next": next_url})}">
{wordmark(88, cls="splash__mark")}
<p class="splash__line">Let the agent shop.<br>You still hold the leash.</p>
<span class="splash__tap">Tap to continue</span>
<span class="splash__by">by <b>one</b></span></a>"""
    return _document("LEASH by one", body, root=root, warm=True)


def login_page(personas: Sequence[Persona], *, next_url: str, root: str, error: str = "") -> str:
    rows = "".join(
        f"""<label class="prow"><input type="radio" name="subject" value="{esc(p.customer_id)}" {"checked" if i == 0 else ""}>
<span class="row__body"><span class="row__k">{esc(p.name)}</span><span class="row__s">{esc(p.card_label)} •••• {esc(p.last4)} · {p.purchases} purchases on record</span></span></label>"""
        for i, p in enumerate(personas)
    )
    err = f'<p class="err">{esc(error)}</p>' if error else ""
    body = f"""{_header(back=f'<a class="hd__back" href="{esc(root)}/" aria-label="Back">{ICONS["back"]}</a>')}
<form class="step" method="post" action="{esc(root)}/login"><input type="hidden" name="next" value="{esc(next_url)}">
<p class="eyebrow">Sign in</p><h1 class="ttl">Who is connecting?</h1>
<p class="lead">Pick the cardholder. A mock: the personas come from the data pack and any PIN works.</p>{err}
<section class="card">{rows}</section>
<div class="acts"><input class="pin" name="pin" inputmode="numeric" placeholder="PIN" autocomplete="off" aria-label="PIN">
<button class="btn btn--primary" type="submit">Sign in</button>
<p class="note">No credit check. No effect on your card.</p></div></form>"""
    return _document("one — sign in", body, root=root)


# ------------------------------------------------------------------ the onboarding


#: The four purchases of step 4, as the storyboard states them. The amounts are the
#: storyboard's; the known shop is the cardholder's own most-used one.
CASES: tuple[tuple[str, str, str, str], ...] = (
    ("A", "CHF 35 at {shop}", "Known merchant", "allow"),
    ("B", "CHF 450 at {shop}", "Known merchant, higher amount", "ask"),
    ("C", "CHF 450 at a new merchant", "Merchant not known yet", "ask"),
    ("D", "Concert ticket up to CHF 120", "Time-critical purchase during presale", "ask"),
)


def onboarding_page(
    *,
    agent_name: str,
    persona: Persona,
    scopes: Sequence[tuple[str, str]],
    params: dict[str, str],
    root: str,
    onboarded: bool,
) -> str:
    """Every step on one page; ``static/onboarding.js`` shows one at a time. A cardholder who
    has been through it starts at the last step, which is the consent itself."""
    agent = esc(agent_name)
    pattern = persona.pattern
    cap = int(pattern["suggested_cap_chf"])
    shop = esc(pattern.get("top_shop") or "a shop you know")
    hidden = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in params.items() if v
    )
    asked = "".join(_row("doc", esc(sentence), tone="grey") for _, sentence in scopes)
    cases = "".join(
        f"""<div class="case" data-case="{letter}"><div class="case__hd"><span class="badge">{letter}</span>
<span class="row__body"><span class="row__k">{esc(title.format(shop=shop))}</span><span class="row__s">{esc(sub)}</span></span></div>
<div class="seg" role="group" aria-label="{esc(title.format(shop=shop))}">
<button class="seg__b" type="button" data-choice="allow" aria-pressed="{"true" if default == "allow" else "false"}">Allow automatically</button>
<button class="seg__b" type="button" data-choice="ask" aria-pressed="{"true" if default == "ask" else "false"}">Ask me first</button></div></div>"""
        for letter, title, sub, default in CASES
    )
    start = "ready" if onboarded else "intro"

    intro = f"""<section class="step" data-step="intro" hidden>
{_header(back='<button class="hd__back" type="submit" name="decision" value="deny" aria-label="Not now">' + ICONS["back"] + "</button>")}
<div class="card hero"><div class="hero__mark">{signet(48)}</div>
<h1 class="ttl">Leash protects your payments</h1>
<p class="lead">Your AI agent can search, compare and shop for you. Leash makes sure it only pays within the rules you set.</p></div>
<section class="card">{_row("bag", "Your agent does the shopping", "Search, compare, pay", tone="grey")}
{_row("user", "You set the rules", "You decide what goes through automatically", tone="grey")}
{_row("shield", "Leash checks every payment", "If something does not fit, Leash asks you first", tone="grey")}</section>
<div class="acts"><button class="btn btn--primary" type="button" data-go="1">Set up Leash</button>
<p class="note">No credit check. No effect on your card.</p></div></section>"""

    step1 = f"""<section class="step" data-step="1" hidden>
{_header(back=_back_button(**{"data-back": "1"}))}
<p class="eyebrow">Step 1 of 5</p><h1 class="ttl">Leash uses your past payments</h1>
<p class="lead">To suggest starting rules that fit you, Leash looks at how you have paid so far.</p>
<section class="card"><p class="eyebrow card__eyebrow">Leash looks at, for example</p>
{_row("shop", "Which merchants you buy from")}{_row("clock", "How often you pay")}{_row("bars", "Which amount ranges you buy in")}</section>
<section class="card card--muted">{_row("lock", "Your data is not shared with merchants. You can stop this at any time.", tone="grey")}</section>
<div class="acts"><button class="btn btn--primary" type="button" data-go="2" data-history="yes">Allow</button>
<button class="btn btn--secondary" type="button" data-go="ready" data-history="no">Later</button>
<p class="note">No credit check. No effect on your card.</p></div></section>"""

    step2 = f"""<section class="step" data-step="2" hidden>
{_header(back=_back_button(**{"data-back": "1"}))}
<p class="eyebrow">Step 2 of 5</p><h1 class="ttl">How you usually pay</h1>
<p class="lead">Leash found a pattern in your payments. It does not describe you as a person, only how you use your card.</p>
<section class="card"><div class="pattern"><span class="pattern__ico">{ICONS["scales"]}</span>
<div><p class="eyebrow">Shopping pattern</p><p class="pattern__txt">{esc(pattern["sentence"])}</p></div></div>
<div class="divider"></div><p class="eyebrow card__eyebrow">How Leash can tell</p>
{_kv("Purchases, last 3 months", str(pattern["purchases_90d"]))}
{_kv("At merchants you know", f"{pattern['known_90d']} of {pattern['purchases_90d']}")}
{_kv("Typical amount", f"CHF {pattern['typical_low']} to {pattern['typical_high']}")}</section>
<div class="acts"><p class="q">Does this fit you?</p>
<button class="btn btn--primary" type="button" data-go="3">Yes, that fits</button>
<button class="btn btn--secondary" type="button" data-go="4">Adjust</button>
<p class="note">Nothing is active until you confirm.</p></div></section>"""

    step3 = f"""<section class="step" data-step="3" hidden>
{_header(back=_back_button(**{"data-back": "1"}))}
<p class="eyebrow">Step 3 of 5</p><h1 class="ttl">This is your suggested leash</h1>
<p class="lead">Based on what you told us, Leash suggests these rules:</p>
<section class="card">{_row("shop", "Known merchants", f"Allow up to CHF {cap} per purchase automatically")}
{_row("plus", "New merchants", "Always ask first")}
{_row("trend", "Higher amounts", f"Ask first from CHF {cap}")}
{_row("calendar", "Time-limited purchases", "For example tickets or travel only after your confirmation")}</section>
<div class="acts"><button class="btn btn--primary" type="button" data-go="4">Continue</button>
<button class="btn btn--secondary" type="button" data-go="4">Adjust rules</button>
<p class="note">You can change every rule. Nothing is active until you confirm.</p></div></section>"""

    step4 = f"""<section class="step" data-step="4" hidden>
{_header(back=_back_button(**{"data-back": "1"}))}
<p class="eyebrow">Step 4 of 5</p><h1 class="ttl">What should Leash do for you?</h1>
<p class="lead">Your AI agent wants to pay. Decide whether Leash may allow the purchase automatically.</p>
{cases}
<div class="acts"><button class="btn btn--primary" type="button" data-go="ready">Continue</button></div></section>"""

    ready = f"""<section class="step" data-step="ready" hidden>
{_header(back=_back_button(**{"data-back": "1"}))}
<div class="card hero"><div class="hero__mark">{signet(48)}</div><p class="eyebrow">Step 5 of 5</p>
<h1 class="ttl consent__ttl">Your leash is ready</h1>
<p class="lead"><b>{agent}</b> may search, compare and pay with your {esc(persona.card_label)} card ending <b>{esc(persona.last4)}</b>, within these rules:</p></div>
<section class="card">{_row("check", f"Up to CHF {cap}", "at known merchants, automatically")}
{_row("info", "New merchants or higher amounts", "Leash asks you first")}
{_row("user", "Everything stays under your control", "You can adjust, pause or turn off Leash at any time.")}
{_row("shield", f"{agent} can never approve its own requests, confirm a mandate, or change your limits.", "", tone="orange")}</section>
<section class="card card--muted"><p class="eyebrow card__eyebrow">{agent} asked to</p>{asked}</section>
<div class="acts"><button class="btn btn--primary" type="submit" name="decision" value="approve">Confirm Leash</button>
<button class="btn btn--ghost" type="submit" name="decision" value="deny">Not now</button>
<p class="note">Active until you change it. No permanent authorisation.</p></div></section>"""

    body = (
        f'<form id="consent" method="post" action="{esc(root)}/authorize" data-start="{start}">'
        f'{hidden}<input type="hidden" name="setup" value="">'
        f"{intro}{step1}{step2}{step3}{step4}{ready}</form>"
    )
    config = {"cap": cap, "agent": agent_name, "start": start}
    scripts = (
        f"<script>window.WALLET = {json.dumps(config)};</script>"
        f'<script type="module" src="{esc(root)}/static/onboarding.js"></script>'
    )
    return _document("one — connect an agent", body, root=root, scripts=scripts)


# ------------------------------------------------------------------ the cockpit


def cockpit_page(*, persona: Persona, config: dict[str, Any], root: str) -> str:
    """The shell; ``static/cockpit.js`` draws every screen into it."""
    boot = {
        **config,
        "icons": ICONS,
        "signet": {"small": signet(24), "hero": signet(30)},
        "persona": persona.as_dict(),
    }
    body = '<div class="step" data-app></div><div class="toasts" data-toasts></div>'
    scripts = (
        f"<script>window.WALLET = {json.dumps(boot)};</script>"
        f'<script type="module" src="{esc(root)}/static/cockpit.js"></script>'
    )
    return _document("one — Cockpit", body, root=root, scripts=scripts)
