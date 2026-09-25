"""Orchestration: run state, the live run loop, and the supervisor that owns them.

Sits between the pure engine (``domain/``) and the HTTP surfaces (``service/``, ``tools/``).
It composes — it decides nothing. Every verdict still comes from ``domain.evaluator``.
"""
