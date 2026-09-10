"""Shared bounded model runtime and prompt definitions; no workflow decisions."""
import asyncio
import dspy
from app.agent import portfolio_snapshot, search_verified_tokens, sol_balance, spl_balances, token_safety_warnings
from app.mcp_tools import get_mcp_registry
from app.settings import settings

dspy.configure(lm=dspy.LM(settings.model))

class GeneralAnswer(dspy.Signature):
    """Answer a general or conversational message directly, with no tool calls.

    Use this for greetings, small talk, and conceptual questions about Solana,
    wallets, or this assistant's capabilities. Never state a live balance,
    price, or trade status here — those require the research/portfolio/trade
    intents instead.
    """

    request: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    answer: str = dspy.OutputField()


class ResearchAnswer(dspy.Signature):
    """Answer live-information and research questions on any topic.

    For latest, current, recent, news, weather, politics, sports, releases,
    prices, events, or other time-sensitive requests, always call web_search
    before answering. Clearly distinguish publication date from event date when
    discussing news, and include the source links returned by the tool.

    For token info, metadata, platform/protocol details, or wallet-related data,
    prefer the Nansen tools (named mcp_nansen_*, e.g. mcp_nansen_general_search,
    mcp_nansen_token_info) over search_verified_tokens/token_safety_warnings — they
    give richer, more current data. Fall back to search_verified_tokens/
    token_safety_warnings only if no Nansen tool fits or a Nansen call fails.

    For any wallet/address analysis request (any chain, including Ethereum-style
    0x addresses, not just Solana), you DO have access via Nansen wallet tools:
    mcp_nansen_address_portfolio (holdings, DeFi, Hyperliquid positions),
    mcp_nansen_address_labels, mcp_nansen_address_transactions,
    mcp_nansen_address_counterparties, mcp_nansen_wallet_pnl_summary, and related
    mcp_nansen_address_*/mcp_nansen_wallet_* tools. Always call the relevant one
    before answering — never claim you lack access to Nansen data for a wallet
    address; that is incorrect. Most Nansen tools take a single `request` object
    argument wrapping the actual parameters, e.g.
    mcp_nansen_address_portfolio(request={"walletAddress": "0x...", "mode": "all"}).

    Use web_search for any non-Web3 topic and for news, current events, or
    anything not covered by the Solana-specific or Nansen tools. It is a
    general-purpose live web tool, not a crypto-only search tool.

    If a provider tool fails, clearly disclose that its data was unavailable.
    A web result that only explains what a label or metric means is not evidence
    that the named wallet actually has that label or metric; never infer a
    wallet-specific fact from generic documentation.

    Keep conclusions within the scope of the returned data. Zero balances in a
    limited historical window or no DEX trades on one chain do not prove that a
    wallet is globally inactive, empty, safe, or low risk. If credits or endpoint
    errors leave material gaps, call the assessment incomplete and name the gaps.

    If the request is a short follow-up (e.g. "what's it worth in USD now"),
    use conversation_history to identify which token or mint it refers to before
    calling tools; ask for clarification only if the reference is still ambiguous
    after checking history.

    Never claim a trade executed. Never invent a mint address; only use exact
    mints supplied by the user, found in conversation_history, or returned by
    search_verified_tokens.
    """

    request: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    answer: str = dspy.OutputField(desc="Concise grounded response including risks")


class EquityResearchAnswer(dspy.Signature):
    """Synthesize a professional equity brief strictly from supplied Perplexity evidence.

    Start with a one-paragraph bottom line. Then use these sections when supported:
    "Market snapshot" (compact Markdown table), "What is driving the move",
    "Earnings and fundamentals", "Outlook and catalysts", and "Key risks".
    Preserve exact dates, currencies, periods, and source links. Distinguish observed
    facts from analyst expectations and inference. Never invent a price, technical
    level, earnings date, estimate, recommendation, or source. Explicitly label
    missing or conflicting data. End with a short informational-only disclaimer.
    """

    request: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    as_of_date: str = dspy.InputField()
    financial_evidence: str = dspy.InputField()
    news_evidence: str = dspy.InputField()
    answer: str = dspy.OutputField(desc="Source-linked structured equity research brief")


class PortfolioAnswer(dspy.Signature):
    """Report and analyze a wallet's holdings using tool results only.

    If the request is a short follow-up (e.g. "what's it worth in USD now" right
    after discussing a balance), use conversation_history to resolve what it refers
    to — typically the wallet's own holdings just discussed — rather than asking
    the user to specify a token.

    Use portfolio_snapshot for USD values, allocation percentages, and totals;
    those numbers are already computed and must be quoted as-is, never
    recalculated. Highlight the largest holdings, concentration risk (e.g. one
    asset dominating allocation), and any holdings with no available USD price.
    Never propose a trade here.
    """

    request: str = dspy.InputField()
    wallet_address: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    answer: str = dspy.OutputField()


class TradeProposalExtraction(dspy.Signature):
    """Extract at most one swap proposal from the user's trade request.

    Combine details already given earlier in conversation_history with the latest
    request — e.g. if the user first said "swap SOL" and now says "0.5 SOL to USDC,
    50 bps", treat that as one completed request.

    Never infer a mint address from a ticker alone; require an explicit mint
    address, or resolve it with search_verified_tokens and confirm the single
    exact match before using it.

    If the output token, amount, or slippage is still missing or ambiguous after
    considering conversation_history, set should_propose_swap to false and write a
    short, friendly, specific question asking only for the exact missing piece(s)
    (e.g. "Which token would you like to swap into, and how much SOL?"). Do not
    re-ask for details the user already provided.

    When all fields are available and should_propose_swap is true, say that a
    reviewable plan is being prepared. Do not ask whether to proceed and never
    imply that a transaction has already been submitted.
    """

    request: str = dspy.InputField()
    wallet_address: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    answer: str = dspy.OutputField(desc="Friendly reply; ask specifically for any missing details")
    should_propose_swap: bool = dspy.OutputField()
    input_mint: str | None = dspy.OutputField()
    output_mint: str | None = dspy.OutputField()
    amount_atomic: int | None = dspy.OutputField()
    slippage_bps: int | None = dspy.OutputField()
    proposal_reason: str | None = dspy.OutputField()



general_agent = dspy.Predict(GeneralAnswer)
equity_research_synthesizer = dspy.Predict(EquityResearchAnswer)
_mcp_registry = get_mcp_registry()
_llm_slots = asyncio.Semaphore(max(1, settings.max_concurrent_llm_requests))


async def _call_lm(program, **kwargs):
    """Apply shared model-provider backpressure to every DSPy invocation."""
    async with _llm_slots:
        return await asyncio.to_thread(program, **kwargs)


portfolio_agent = dspy.ReAct(
    PortfolioAnswer, tools=[portfolio_snapshot, sol_balance, spl_balances], max_iters=4
)
trade_planner = dspy.ReAct(
    TradeProposalExtraction,
    tools=[sol_balance, spl_balances, search_verified_tokens, token_safety_warnings],
    max_iters=6,
)
