from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
import asyncio
import re
from app.context_entities import extract_token_reference
from app.portfolio import build_portfolio_snapshot
from app.provider_registry import get_provider_router
from app.wallet_insights import portfolio_scenario, wallet_health
from app.nodes.research import _nansen_wallet_tool_call, _provider_trajectory, call_direct_mcp_tool

@trace(name="portfolio", as_type="agent")
async def portfolio_node(state: AgentState) -> dict:
    if not state.get("wallet_address"):
        return {"answer": "I need a wallet address to check balances or analyze a portfolio.", "trajectory": None}
    capabilities = set(state.get("capabilities", []))
    request = _effective_request(state)
    if "wallet_transactions" in capabilities:
        wallet = state["wallet_address"]
        chains = tuple(state.get("chains", []))
        if wallet.startswith("0x") and not chains:
            return {
                "answer": "Which EVM chain should I check for your connected wallet’s transactions, for example Base, Ethereum, or Arbitrum?",
                "trajectory": None,
            }
        chain = chains[0] if chains else "solana"
        # Nansen is the primary on-chain-intelligence source for the connected
        # wallet's activity; GoldRush/Helius (via the capability router) are
        # tried only if Nansen isn't discovered or its call fails.
        nansen = _nansen_wallet_tool_call(wallet, chain, "recent transactions activity")
        if nansen is not None:
            tool_name, arguments = nansen
            observation = await call_direct_mcp_tool(tool_name, arguments)
            if not observation.startswith("MCP tool call failed:"):
                return {
                    "answer": observation,
                    "trajectory": {
                        "thought_0": "Nansen is the primary source for connected-wallet on-chain activity.",
                        "tool_name_0": tool_name,
                        "tool_args_0": arguments,
                        "observation_0": observation,
                    },
                }
            # Nansen failed or exceeded the fast-path timeout; fall through.
        provider_request = f"Show recent wallet transactions for {wallet} on {chain}"
        try:
            result = await asyncio.to_thread(
                get_provider_router().route,
                provider_request,
                "wallet_intelligence",
                (chain,),
            )
            return {
                "answer": result.output,
                "trajectory": _provider_trajectory(result, provider_request, "wallet_intelligence"),
            }
        except RuntimeError as exc:
            return {
                "answer": (
                    f"I couldn’t retrieve the connected wallet’s recent {chain.title()} transactions "
                    "because neither Nansen nor a configured wallet-activity provider succeeded. "
                    "No web-search result was substituted."
                ),
                "trajectory": {
                    "thought_0": "Use only an on-chain wallet activity provider for connected-wallet transactions.",
                    "tool_name_0": "wallet_activity_router",
                    "tool_args_0": {"wallet_address": wallet, "chain": chain},
                    "observation_0": f"Wallet activity lookup failed: {exc}",
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
    result = await runtime._call_lm(
        runtime.portfolio_agent,
        request=request,
        wallet_address=state["wallet_address"],
        conversation_history=state.get("history", ""),
    )
    return {"answer": result.answer, "trajectory": getattr(result, "trajectory", None)}
