from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
import asyncio
import re
from app.context_entities import extract_token_reference
from app.plans import simulate_swap
from app.portfolio import build_portfolio_snapshot
from app.provider_registry import get_provider_router
from app.trade_context import complete_swap_fields
from app.wallet_insights import portfolio_scenario, wallet_health
from app.nodes.research import _compose_wallet_portfolio, _goldrush_hyperliquid_supplement, _nansen_wallet_tool_call, _provider_trajectory, _sanitize_react_answer, call_direct_mcp_tool
from app import handles

async def _perp_positions(wallet: str, request: str) -> dict:
    """Open perps for a wallet, from the focused Hyperliquid positions tool.

    Hyperliquid accounts are EVM (0x) addresses. A non-EVM wallet with a
    perps ask is ambiguous -- a Solana wallet has no Hyperliquid account, and
    guessing another venue would answer a question the user did not ask --
    so it is a question back, not a substitution."""
    if not wallet.startswith("0x"):
        return {
            "answer": (f"Perp positions on Hyperliquid belong to an EVM (0x) address, and `{wallet[:6]}…{wallet[-4:]}` "
                       "is not one. Did you mean a different wallet, or perps on another venue? Tell me which and I'll look it up."),
            "trajectory": None,
        }
    positions = await _goldrush_hyperliquid_supplement(wallet)
    answer = positions or (f"# Hyperliquid positions\n\nI couldn't retrieve Hyperliquid account state for `{wallet}` right now "
                           "(the Hyperliquid data source was unavailable). Please try again shortly.")
    return {
        "answer": answer,
        "trajectory": {
            "thought_0": "A perp-positions ask about a wallet maps to the focused Hyperliquid positions tool.",
            "tool_name_0": "goldrush_hyperliquid_positions", "tool_args_0": {"wallet_address": wallet}, "observation_0": answer,
        },
    }


@trace(name="portfolio", as_type="agent")
async def _verified_mint(symbol: str) -> str | None:
    """The Solana mint of a ticker from Jupiter's verified list: the one
    verified token with exactly that symbol, else None (never a guess among
    namesakes)."""
    known = {"USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "SOL": "So11111111111111111111111111111111111111112"}
    if symbol.upper() in known:
        return known[symbol.upper()]                                  # the stablecoins and SOL need no lookup (a rate-limited search asked for a wallet instead)
    from app.jupiter import jupiter
    try:
        rows = await jupiter.search_tokens(symbol)
    except Exception:
        return None
    exact = [r for r in rows or [] if str(r.get("symbol") or "").upper() == symbol.upper()]
    verified = [r for r in exact if "verified" in [str(t).lower() for t in (r.get("tags") or [])]]
    if not verified:
        return None                                                  # an unverified namesake is never a silent pick (review of bc40f724)
    verified.sort(key=lambda r: float(r.get("organicScore") or 0), reverse=True)
    top = verified[0]
    if len(verified) > 1 and float(verified[1].get("organicScore") or 0) >= float(top.get("organicScore") or 0) * 0.5:
        return None                                                  # two comparable namesakes: not for a silent pick
    return top.get("id")


async def _portfolio_node(state: AgentState) -> dict:
    handle = handles.social_handle(_effective_request(state))
    if handle:
        # A handle names someone else's wallet, which Orbit cannot resolve;
        # the connected wallet is not what was asked about (live, 2026-09-21).
        return {"answer": handles.answer_for(handle), "trajectory": None}
    capabilities = set(state.get("capabilities", []))
    request = _effective_request(state)
    # A hypothetical amount needs no wallet: "How much USDC would 0.05 SOL
    # get? Just estimate." is a read-only Jupiter quote of the stated size
    # (expanded UI review, 2026-09-24: it asked to connect a wallet).
    hypothetical = not state.get("wallet_address") and "trade_simulation" in capabilities and bool(re.search(r"\b\d+(?:\.\d+)?\s*[A-Za-z]{2,10}\b", request))
    if hypothetical:
        state = {**state, "wallet_address": ""}
    if not state.get("wallet_address") and not hypothetical and "trade_simulation" in capabilities:
        # "I only want a read-only estimate" after an exit ask with no wallet:
        # the estimate needs a size, not a wallet (expanded journeys,
        # 2026-09-24: it asked to connect a wallet again).
        focus = (state.get("session_context") or {}).get("focus") or {}
        from app import contracts
        f = contracts.plan_by_rules(request).filters or {}
        symbol = f.get("input_token") or (focus.get("label") if focus.get("label") and focus.get("label") != "TOKEN" else None) or "the token"
        return {"answer": (f"No wallet is needed for an estimate, only a size. How much {symbol}: a token amount (`estimate selling 1000 {symbol} to USDC`) "
                           f"or a dollar size (`what would exiting $1,000 of {symbol} cost`)? I'll quote it read-only; nothing is prepared or submitted."),
                "trajectory": None}
    if not state.get("wallet_address") and not hypothetical:
        # Parked like a swap without a wallet: "connected" on the next turn
        # re-runs this request instead of falling through to a clarification.
        return {
            "answer": ("I need a wallet to check balances or analyze a portfolio. Connect one with **Connect wallet** "
                       "(or paste a public address), then reply `connected` and I'll run this check."),
            "trajectory": None,
            "pending_wallet_request": state["request"],
        }
    capabilities = set(state.get("capabilities", []))
    request = _effective_request(state)
    if "perp_positions" in capabilities:
        return await _perp_positions(state["wallet_address"], request)
    if "wallet_transactions" in capabilities:
        wallet = state["wallet_address"]
        chains = tuple(state.get("chains", []))
        if wallet.startswith("0x") and not chains:
            return {
                "answer": "Which EVM chain should I check for your connected wallet’s transactions, for example Base, Ethereum, or Arbitrum?",
                "trajectory": None,
            }
        chain = chains[0] if chains else "solana"
        # GoldRush/Helius (via the capability router) are already chain-scoped
        # and prioritized above Nansen for wallet activity, so try them first;
        # Nansen is the last-resort fallback if the router has nothing configured.
        provider_request = f"Show recent wallet transactions for {wallet} on {chain}"
        result = await asyncio.to_thread(
            get_provider_router().try_route,
            provider_request,
            "wallet_intelligence",
            (chain,),
        )
        if result is not None:
            return {
                "answer": result.output,
                "trajectory": _provider_trajectory(result, provider_request, "wallet_intelligence"),
            }
        router_error = "No configured wallet-activity provider succeeded"
        nansen = _nansen_wallet_tool_call(wallet, chain, "recent transactions activity")
        if nansen is not None:
            tool_name, arguments = nansen
            observation = await call_direct_mcp_tool(tool_name, arguments)
            if not observation.startswith("MCP tool call failed:"):
                return {
                    "answer": observation,
                    "trajectory": {
                        "thought_0": "No configured wallet-activity provider succeeded; falling back to Nansen.",
                        "tool_name_0": tool_name,
                        "tool_args_0": arguments,
                        "observation_0": observation,
                    },
                }
            # Nansen failed or exceeded the fast-path timeout too.
            router_error = f"{router_error}; Nansen fallback: {observation}"
        return {
            "answer": (
                f"I couldn’t retrieve the connected wallet’s recent {chain.title()} transactions "
                "because neither a configured wallet-activity provider nor Nansen succeeded. "
                "No web-search result was substituted."
            ),
            "trajectory": {
                "thought_0": "Use only an on-chain wallet activity provider for connected-wallet transactions.",
                "tool_name_0": "wallet_activity_router",
                "tool_args_0": {"wallet_address": wallet, "chain": chain},
                "observation_0": f"Wallet activity lookup failed: {router_error}",
            },
        }
    if state["wallet_address"].startswith("0x") and capabilities & {"token_balance", "token_holdings", "wallet_health"}:
        # The snapshot below reads Solana RPC; an EVM wallet is composed the
        # way a pasted EVM address is (balances per chain, Hyperliquid, DeFi).
        if "wallet_health" in capabilities:
            return {"answer": ("Wallet health checks read Solana balances, and your connected wallet is an EVM (0x) address. "
                               "Ask for its portfolio instead, or connect a Solana wallet for the health check."), "trajectory": None}
        answer, trajectory = await _compose_wallet_portfolio(state["wallet_address"], next(iter(state.get("chains") or []), None))
        return {"answer": answer, "trajectory": trajectory}
    if "token_balance" in capabilities:
        snapshot = await build_portfolio_snapshot(state["wallet_address"])
        reference = extract_token_reference(request)
        asset_match = re.search(
            r"\bhow\s+much(?:\s+of)?\s+\$?([A-Za-z][A-Za-z0-9._-]{1,15})\s+(?:do\s+)?i\s+have\b"
            r"|\bmy\s+\$?([A-Za-z][A-Za-z0-9._-]{1,15})\s+balance\b"
            r"|\bbalance\s+of\s+(?:my\s+)?\$?([A-Za-z][A-Za-z0-9._-]{1,15})\b"
            r"|\bdo\s+i\s+have(?:\s+any)?\s+\$?([A-Za-z][A-Za-z0-9._-]{1,15})\b",
            request,
            re.IGNORECASE,
        )
        symbol = next((item for item in (asset_match.groups() if asset_match else ()) if item), None)
        if symbol and symbol.lower() in {"this", "token", "coin"}:
            symbol = None
        holdings = list(snapshot.get("holdings", []))
        holding = next(
            (
                item
                for item in holdings
                if (symbol and str(item.get("symbol", "")).lower() == symbol.lower())
                or (reference and str(item.get("mint", "")) == reference.address)
            ),
            None,
        )
        requested = symbol or (reference.address if reference else "that token")
        if requested.upper() == "SOL":
            holding = {"symbol": "SOL", **snapshot["sol"]}
        if holding is None and snapshot.get("unread_programs"):
            answer = (f"I could not read {requested}: the {', '.join(snapshot['unread_programs'])} accounts did not answer, so the snapshot is partial. "
                      "Not a zero balance.")
        elif holding is None:
            answer = f"Your connected wallet currently has **0 {requested}** in its reported Solana balances (both token programs read)."
        else:
            value = holding.get("usd_value")
            value_text = f" (approximately **${float(value):,.2f}**)" if value is not None else ""
            answer = (
                f"Your connected wallet currently holds **{float(holding['amount']):,.8g} "
                f"{holding.get('symbol') or requested}**{value_text}."
            )
        return {
            "answer": answer,
            "trajectory": {
                "thought_0": "Read the connected wallet portfolio and select the requested asset without entering a trade workflow.",
                "tool_name_0": "portfolio_snapshot",
                "tool_args_0": {"wallet_address": state["wallet_address"]},
                "observation_0": snapshot,
            },
        }
    if "token_holdings" in capabilities:
        snapshot = await build_portfolio_snapshot(state["wallet_address"])
        holdings = list(snapshot.get("holdings", []))
        if not holdings and snapshot.get("unread_programs"):
            answer = (f"**Snapshot partial**: the {', '.join(snapshot['unread_programs'])} accounts could not be read, so no SPL holdings can be listed "
                      "right now. This is not an empty wallet; try again in a moment.")
        elif not holdings:
            answer = "Your connected Solana wallet currently has **no SPL token holdings** (both token programs read)."
        else:
            rows = []
            for holding in holdings:
                symbol = holding.get("symbol") or "UNKNOWN"
                amount = float(holding.get("amount") or 0)
                value = holding.get("usd_value")
                value_text = f" · ${float(value):,.2f}" if value is not None else " · unpriced"
                rows.append(f"- **{symbol}:** {amount:,.8g}{value_text}")
            answer = "**SPL token holdings**\n\n" + "\n".join(rows)
        return {
            "answer": answer,
            "trajectory": {
                "thought_0": "Read SPL token accounts from the connected Solana wallet without running token discovery.",
                "tool_name_0": "portfolio_snapshot",
                "tool_args_0": {"wallet_address": state["wallet_address"]},
                "observation_0": snapshot,
            },
        }
    if "wallet_health" in capabilities:
        snapshot = await build_portfolio_snapshot(state["wallet_address"])
        report = wallet_health(snapshot)
        findings = "\n".join(
            f"- **{item['title']}:** {item['detail']}" for item in report["findings"]
        ) or "- No balance, pricing, concentration, or gas-readiness warning was detected."
        checked = "\n".join(f"- {line}" for line in report.get("checked") or [])
        return {
            "answer": f"**Wallet health: {report['status'].title()}**\n\n{findings}\n\n**Checked**\n{checked}\n\nScope: {report['scope']}",
            "trajectory": {"thought_0": "Compute wallet health deterministically from the current portfolio snapshot.", "tool_name_0": "portfolio_snapshot", "tool_args_0": {"wallet_address": state["wallet_address"]}, "observation_0": report},
        }
    if "portfolio_scenario" in capabilities:
        match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", request)
        if not match:
            return {"answer": "Tell me the percentage move to simulate, for example: `What if my portfolio drops 20%?`", "trajectory": None}
        raw = float(match.group(1))
        falling = bool(re.search(r"\b(?:drop|drops|fall|falls|down|decline)\b", request, re.IGNORECASE))
        change = -abs(raw) if falling else raw
        symbol_match = re.search(r"\b(SOL|USDC|USDT|ETH|BTC)\b", request, re.IGNORECASE)
        snapshot = await build_portfolio_snapshot(state["wallet_address"])
        report = portfolio_scenario(snapshot, change, symbol_match.group(1) if symbol_match else None)
        return {
            "answer": (
                f"**Portfolio scenario: {report['target']} {change:+g}%**\n\n"
                f"Current priced value: **${report['current_total_usd']:,.2f}**\n\n"
                f"Projected value: **${report['projected_total_usd']:,.2f}**\n\n"
                f"Estimated change: **${report['portfolio_change_usd']:,.2f}**\n\n"
                f"Assumptions: {report['assumptions']}"
            ),
            "trajectory": {"thought_0": "Apply a deterministic price shock to current priced holdings.", "tool_name_0": "portfolio_scenario", "tool_args_0": {"change_pct": change, "symbol": report["target"]}, "observation_0": report},
        }
    if "trade_simulation" in capabilities:
        from types import SimpleNamespace
        pre = (None, None, None, None)
        if hypothetical:
            # The transaction contract already read the amount and the pair in
            # any wording ("selling 0.05 SOL to USDC", "the minimum USDC I'd
            # get for 0.05 SOL"); the swap fields come from those, in the one
            # form the field completer reads.
            from app import contracts
            f = contracts.plan_by_rules(request).filters or {}
            spelled = f"swap {f['amount']} {f['input_token']} to {f['output_token']}" if f.get("amount") and f.get("input_token") and f.get("output_token") else request
            pre = complete_swap_fields(spelled, state.get("history", ""), None, None, None, None)
            if pre[0] and pre[2] and not pre[1] and f.get("output_token"):
                # The completer takes mints, not tickers: the output ticker is
                # resolved against Jupiter's verified list, exact symbol only.
                pre = (pre[0], await _verified_mint(f["output_token"]), pre[2], pre[3])
        if hypothetical and pre[0] and pre[1] and pre[2]:
            # No wallet and a stated amount: the request's own words carry the
            # swap ("0.05 SOL to USDC"), so the extractor and its balance read
            # are skipped (it asked for a wallet it did not need, 2026-09-24).
            result = SimpleNamespace(answer="", trajectory=None, input_mint=pre[0], output_mint=pre[1], amount_atomic=pre[2], should_simulate=True)
        else:
            result = await runtime.answer(
                runtime.trade_simulator,
                request=request,
                wallet_address=state["wallet_address"],
                conversation_history=state.get("history", ""),
            )
        answer = _sanitize_react_answer(result.answer)
        trajectory = getattr(result, "trajectory", None)
        # complete_swap_fields is the same boundary the real trade path uses
        # (app/nodes/trading.py) -- verified live this fix was needed: a
        # DSPy ReAct output can literally be the string "null" rather than
        # Python None, and doesn't reliably substitute the native-SOL mint
        # constant on its own even when it correctly reasons about SOL in
        # its own trajectory.
        input_mint, output_mint, amount_atomic, _slippage = complete_swap_fields(
            request, state.get("history", ""), result.input_mint, result.output_mint, result.amount_atomic, None,
        )
        if hypothetical and not (input_mint and output_mint and amount_atomic):
            # No wallet and a stated amount: the request's own words carry the
            # swap ("0.05 SOL to USDC"); the extractor asked for a wallet to
            # read a balance it does not need (expanded UI review, 2026-09-24).
            input_mint, output_mint, amount_atomic, _slippage = complete_swap_fields(request, state.get("history", ""), None, None, None, None)
        if hypothetical and input_mint and output_mint and amount_atomic:
            result = SimpleNamespace(should_simulate=True)
        if result.should_simulate and input_mint and output_mint and amount_atomic:
            try:
                sim = await simulate_swap(input_mint, output_mint, amount_atomic)
            except Exception as exc:
                answer = f"I couldn't compute a simulated quote: {exc}. No trade was prepared or submitted."
            else:
                in_value = f" (${sim['input_value_usd']:,.2f})" if sim["input_value_usd"] is not None else ""
                out_value = f" (${sim['output_value_usd']:,.2f})" if sim["output_value_usd"] is not None else ""
                out_amount = f"{sim['output_amount']:,.6g}" if sim["output_amount"] is not None else "an unknown amount of"
                answer = (
                    f"**This is a simulation only -- nothing has been prepared or submitted.**\n\n"
                    f"Selling **{sim['input_amount']:,.6g} {sim['input_token'].symbol}**{in_value} would get you "
                    f"approximately **{out_amount} {sim['output_token'].symbol}**{out_value} at the current Jupiter "
                    f"quote, with an estimated **{sim['price_impact_pct']:.2f}% price impact**.\n\n"
                    f"This is a live quote, not a guarantee -- the actual amount at execution time can differ with "
                    f"market movement and slippage."
                )
                trajectory = trajectory or {}
                # sim's input_token/output_token are TokenInfo model instances and
                # quote is Jupiter's raw response -- both must become plain
                # JSON-serializable data before landing in trajectory, which gets
                # json.dumps()'d into session history (verified live: an
                # unconverted TokenInfo instance here broke chat history storage
                # with "Object of type TokenInfo is not JSON serializable").
                trajectory = {
                    **trajectory,
                    "tool_name_sim": "jupiter_simulate_swap",
                    "tool_args_sim": {"input_mint": input_mint, "output_mint": output_mint, "amount_atomic": amount_atomic},
                    "observation_sim": {
                        "input_token": sim["input_token"].model_dump(),
                        "output_token": sim["output_token"].model_dump(),
                        "input_amount": sim["input_amount"],
                        "input_value_usd": sim["input_value_usd"],
                        "output_amount": sim["output_amount"],
                        "output_value_usd": sim["output_value_usd"],
                        "price_impact_pct": sim["price_impact_pct"],
                    },
                }
        return {"answer": answer, "trajectory": trajectory}
    result = await runtime.answer(
        runtime.portfolio_agent,
        request=request,
        wallet_address=state["wallet_address"],
        conversation_history=state.get("history", ""),
    )
    return {"answer": _sanitize_react_answer(result.answer), "trajectory": getattr(result, "trajectory", None)}


async def portfolio_node(state: AgentState) -> dict:
    """The portfolio answer, then the proof: a portfolio contract
    (app/contracts.plan_by_rules) checked against the node's own cards --
    the holdings shown are the wallet asked about, the prose's figures trace
    to the card, a figure that traces to nothing withholds the prose. The
    same gate every contract answer passes, for the kind the node answers
    itself (2026-09-24)."""
    result = await _portfolio_node(state)
    from app.settings import settings as _settings
    if not _settings.contract_pipeline_enabled or not result.get("answer") or result.get("pending_wallet_request") or not state.get("wallet_address"):
        return result
    from app import contracts, evidence_pipeline
    contract = contracts.plan_by_rules(_effective_request(state))
    if contract.kind != "portfolio":
        return result
    trajectory = result.get("trajectory") or {}
    cards = []
    for key, value in trajectory.items():
        if not key.startswith("observation"):
            continue
        if isinstance(value, str) and "|" in value:
            cards.append(value)
        elif isinstance(value, dict) and ("holdings" in value or "sol" in value):
            # The branch recorded the snapshot itself; the proof reads the
            # card the user sees, rendered from it (a correct analysis was
            # withheld against "Completed." on the first live run, 2026-09-24).
            from app.portfolio import render_card
            rendered = render_card(value)
            if rendered:
                cards.append(rendered.replace("# Wallet token balances", f"# Wallet token balances — {state['wallet_address'][:6]}…{state['wallet_address'][-4:]}", 1))
    if not cards:
        return result                                          # nothing to prove against; never withhold without a card
    import json
    exact = json.dumps([v for k, v in trajectory.items() if k.startswith("observation") and isinstance(v, dict)], default=str)
    proved = evidence_pipeline.prove(contract, result["answer"], cards, wallet=state["wallet_address"], extra_evidence=exact)
    return {**result, **proved}
