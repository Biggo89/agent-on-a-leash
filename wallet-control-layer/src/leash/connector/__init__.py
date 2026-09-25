"""Leash Wallet, the plug-and-play connector: how an AI agent is let onto the leash.

An agent host (Claude, ChatGPT, Claude Code) adds one URL. The cardholder logs into their
payment app, reads a consent screen written as a mandate, and the agent has five tools —
propose a mandate, ask what is allowed, request a payment, check one, list recent activity —
and never the verbs that belong to the cardholder: confirm, answer, amend, revoke.

    auth.py     who is calling (RFC 7662 introspection against the app) and the three scopes
    orders.py   an agent's order → the authorization.request the engine already judges
    errand.py   the agent's run: same RunSession as a scenario run, fed by tool calls
    render.py   the words an agent reads; never a reason code
    mcp.py      JSON-RPC over Streamable HTTP, by hand; the five tools
    http.py     the routes, mounted on the decision service

Design note: ../../../../connector/README.md. Normative: specs/connector.md.
"""

from .http import STATE, router

__all__ = ["STATE", "router"]
