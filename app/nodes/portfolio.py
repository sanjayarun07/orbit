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
from app.nodes.research import _goldrush_hyperliquid_supplement, _nansen_wallet_tool_call, _provider_trajectory, _sanitize_react_answer, call_direct_mcp_tool

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
async def portfolio_node(state: AgentState) -> dict:
    if not state.get("wallet_address"):
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
        if holding is None:
            answer = f"Your connected wallet currently has **0 {requested}** in its reported Solana balances."
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
        if not holdings:
            answer = "Your connected Solana wallet currently has **no SPL token holdings**."
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
        return {
            "answer": f"**Wallet health: {report['status'].title()}**\n\n{findings}\n\nScope: {report['scope']}",
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
        result = await runtime._call_lm(
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
    result = await runtime._call_lm(
        runtime.portfolio_agent,
        request=request,
        wallet_address=state["wallet_address"],
        conversation_history=state.get("history", ""),
    )
    return {"answer": _sanitize_react_answer(result.answer), "trajectory": getattr(result, "trajectory", None)}
