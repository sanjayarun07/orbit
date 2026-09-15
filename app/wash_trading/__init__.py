"""Wash-trading / wallet-clustering detector for Solana launchpad tokens.

Operator-triggered, not part of the chat graph -- see /admin/wash-trading/*
routes in app/main.py. Phase 1 scope: top-trader concentration stats,
round-trip detection, and single-hop funding-wallet fan-out, persisted to
ClickHouse. See the implementation plan for the full phased scope and cut
lines (full weighted clustering, MEV-cube filtering, multi-hop funding
tracing, and scheduling are explicitly deferred to later phases).
"""
