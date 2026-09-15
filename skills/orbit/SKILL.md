---
name: orbit
description: "Orbit Web3 copilot: research tokens, wallets, protocols and markets with live on-chain evidence, check a wallet's holdings, and prepare Solana or cross-chain swap quotes that the user confirms in their own wallet. Use for any crypto/DeFi question, portfolio check, or swap request."
metadata:
  mcp:
    server: orbit
    transport: streamable-http
    path: /mcp
---

# Orbit

Orbit answers Web3 questions with live evidence and prepares trades it never
signs. Every answer names its data providers and carries a validation summary
(provenance, freshness, grounding). Trades are quotes: the user confirms them in
their own wallet, in the Orbit web UI, never inside this conversation.

## Tools

Everything the web UI can do is here, in four families. Start with
`orbit_chat` for anything phrased in natural language; reach for the
specific tools when you already know exactly what you need.

**Conversation and session**
- `orbit_chat(message, session_id?, wallet_address?)` — one turn of the
  copilot. Send the user's request as written. Returns the answer, the
  `session_id` to reuse for follow-ups, the detected intent, the providers
  used, the validation status, and — when the turn produced a quote — a
  `handoff_url` where the user confirms it. Always pass the `session_id` from
  the previous call so pronouns ("sell half of it") and follow-ups resolve.
- `orbit_connect_wallet(session_id, address)` — attach a **public** wallet
  address (Solana base58 or EVM 0x) to the session for read-only use.
  Orbit never asks for or accepts a private key or seed phrase.
- `orbit_prepare_swap(session_id, amount, input_token, output_token, source_chain, destination_chain?, slippage_bps?)`
  — quote a swap (Jupiter on Solana, Relay across chains); returns the reviewed
  quote and a `handoff_url`. `orbit_trade_simulation(...)` is the read-only
  "what would I get" variant that never creates a plan.
- `orbit_set_team_mode(session_id, enabled)` — the multi-agent trading desk.
- `orbit_set_risk_charter(session_id, max_trade_usd?, max_position_pct?, max_slippage_bps?, verified_only?, allowed_chains?, notes?)`,
  `orbit_clear_risk_charter`, `orbit_risk_charter_limits`, `orbit_policy` —
  the user's exact risk rules, checked on every quote; caps cannot be raised.
- `orbit_history(session_id)`, `orbit_delete_history(session_id)`,
  `orbit_handoff_url(session_id)`.

**Analytics (deterministic, no model involved)**
- `orbit_portfolio`, `orbit_wallet_health`, `orbit_portfolio_scenario(change_pct, symbol?)`
  — the bound wallet's holdings, diagnostics and price-shock simulation.
- `orbit_market_overview()` — the market card; `orbit_token_deep_dive(token, chain?)`
  — ten-dimension due diligence.
- `orbit_trade_plan(plan_id)`, `orbit_execution_status(signature)`,
  `orbit_relay_status(request_id)` — quotes and submitted transactions.
- `orbit_capabilities()`, `orbit_route_preview(request, capability, chains?)`,
  `orbit_health()` — what Orbit can route to, and how it would.

**Data sources, one tool each** — `orbit_data_<provider_tool>` (DexScreener,
GeckoTerminal, Birdeye, Mobula, Bitquery, CoinGecko, CoinMarketCap, GoPlus,
Honeypot.is, DeFiLlama, Dune, Helius, GoldRush, RootData, Perplexity, …). Each
takes a natural-language `request` and optional `chains`, runs through the
same quotas, circuit breakers, cache and budget as the copilot, and returns
that provider's evidence verbatim. `orbit_mcp_catalog()` / `orbit_mcp_call(tool_name, arguments)`
reach the tools Orbit itself discovers from MCP servers (e.g. Nansen);
execution-risk tools are refused.

**Resources**: `orbit://skill` (this playbook), `orbit://capabilities`,
`orbit://health`, `orbit://history/{session_id}`, `orbit://policy/{session_id}`.
**Prompts**: `orbit_skill`, `orbit_deep_dive(token, chain?)`, `orbit_market_brief`.

## How wallet connection works from here

An agent host (Claude, ChatGPT, an IDE) has no wallet and must never hold keys,
so Orbit separates *knowing* a wallet from *signing* with it:

1. **Read-only binding.** `orbit_connect_wallet` stores a public address on the
   session. Everything analytical works from that alone.
2. **Signing stays in the user's wallet.** When a turn produces a quote, Orbit
   persists it as a plan with a short expiry and returns `handoff_url`. Show
   that link. The user opens it, connects Phantom / Coinbase Wallet / MetaMask /
   Privy / WalletConnect in the browser, reviews the card (amount, route, fees,
   price impact, security warnings, risk-charter verdict), and presses Confirm;
   the wallet itself signs. If the quote has expired by then, the UI offers a
   fresh one. The confirmation token is bound to the plan ID and cannot be
   replayed from chat.
3. **What this conversation can never do:** sign, submit, or move funds; raise
   the built-in per-trade, slippage or price-impact caps; or bypass the user's
   risk charter.

## Working rules

- Pass the user's words through; do not translate a question into a trade.
  "Should I buy X?" is advice and Orbit answers it as research.
- Reuse `session_id`. Start a new session only for an unrelated topic.
- If Orbit asks which chain a symbol is on, relay the question and send the
  user's answer back as the next `orbit_chat` message.
- Report validation warnings and evidence gaps to the user rather than
  smoothing them over; do not add figures Orbit did not return.
