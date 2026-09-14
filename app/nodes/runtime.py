"""Shared bounded model runtime and prompt definitions; no workflow decisions."""
import asyncio
import logging
import dspy
from app.agent import portfolio_snapshot, search_verified_tokens, sol_balance, spl_balances, token_safety_warnings
from app.mcp_tools import get_mcp_registry
from app.metrics import increment
from app.repeat_guard import guard_tools, reset_guard, start_guard
from app.settings import settings

logger = logging.getLogger(__name__)

# Every DSPy hop gets an explicit per-request timeout (litellm otherwise waits up
# to 600s on a wedged provider) plus litellm's own exponential-backoff retries
# against the same model. See _call_lm for the cross-provider fallback on top.
_primary_lm = dspy.LM(
    settings.model,
    timeout=settings.llm_request_timeout_seconds,
    num_retries=settings.llm_num_retries,
)
_fallback_lm = (
    dspy.LM(
        settings.llm_fallback_model,
        timeout=settings.llm_request_timeout_seconds,
        num_retries=settings.llm_num_retries,
    )
    if settings.llm_fallback_model
    else None
)
dspy.configure(lm=_primary_lm)

# Provider-side failures worth retrying on a DIFFERENT model (a transient outage
# of the primary provider). A bad request, auth error, or context-length error is
# the same on any model, so it must NOT trigger fallback -- it surfaces directly.
try:
    import litellm

    _TRANSIENT_LM_ERRORS: tuple[type[Exception], ...] = (
        litellm.Timeout,
        litellm.RateLimitError,
        litellm.APIConnectionError,
        litellm.InternalServerError,
        litellm.ServiceUnavailableError,
        litellm.BadGatewayError,
    )
except Exception:  # pragma: no cover - litellm is a dspy dependency, but stay defensive
    _TRANSIENT_LM_ERRORS = ()

_TRANSIENT_LM_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _is_transient_lm_error(exc: Exception) -> bool:
    if _TRANSIENT_LM_ERRORS and isinstance(exc, _TRANSIENT_LM_ERRORS):
        return True
    status = getattr(exc, "status_code", None)
    return status in _TRANSIENT_LM_STATUS

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

    For token info, metadata, or platform/protocol details, prefer the Nansen
    tools (named mcp_nansen_*, e.g. mcp_nansen_general_search,
    mcp_nansen_token_info) over search_verified_tokens/token_safety_warnings — they
    give richer, more current data. Fall back to search_verified_tokens/
    token_safety_warnings only if no Nansen tool fits or a Nansen call fails.

    Wallet balances/holdings and portfolio-composition requests are normally
    answered before you are ever invoked (a deterministic pipeline queries
    GoldRush/Bitquery for balances and Hyperliquid positions, with Nansen only
    as optional DeFi-positions enrichment). You are only reached for a wallet
    request when that pipeline didn't recognize it, so you do not have direct
    access to GoldRush — the tools available to you for wallet/address analysis
    are the Nansen ones: mcp_nansen_address_portfolio (holdings, DeFi,
    Hyperliquid positions), mcp_nansen_address_labels,
    mcp_nansen_address_transactions, mcp_nansen_address_counterparties,
    mcp_nansen_wallet_pnl_summary, and related mcp_nansen_address_*/
    mcp_nansen_wallet_* tools. Call the relevant one before answering, but treat
    a credit-exhaustion or error response as a real gap to disclose, not
    something to paper over or retry into a guess — never claim you lack access
    to Nansen data outright either; attempt the call first. Most Nansen tools
    take a single `request` object argument wrapping the actual parameters, e.g.
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


class TradeSimulationExtraction(dspy.Signature):
    """Extract a hypothetical swap to simulate -- NEVER a real trade proposal.
    This output is only ever used to fetch a read-only Jupiter quote; there is
    no code path from here into transaction building or submission, so it is
    safe to resolve even an ambiguous-sounding request as best-effort.

    Resolve relative amounts ("half", "25%", "all my SOL") against the
    caller's real current balance using the sol_balance/spl_balances tools --
    never guess a balance. Never infer a mint address from a ticker alone;
    require an explicit mint, or resolve it with search_verified_tokens and
    confirm the single exact match before using it.

    If the asset to sell/buy, the counter-asset, or the amount is still
    missing or ambiguous, set should_simulate to false and write a short,
    friendly question asking only for the exact missing piece.

    When should_simulate is true, say a simulated quote is being computed.
    Always make clear this is a hypothetical simulation -- never say a trade
    was placed, prepared, or is awaiting confirmation; nothing is confirmable
    here at all.
    """

    request: str = dspy.InputField()
    wallet_address: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    answer: str = dspy.OutputField(desc="Friendly reply; ask specifically for any missing details")
    should_simulate: bool = dspy.OutputField()
    input_mint: str | None = dspy.OutputField()
    output_mint: str | None = dspy.OutputField()
    amount_atomic: int | None = dspy.OutputField()



class RiskAssessment(dspy.Signature):
    """Judge a fully-quoted, simulated trade plan against the user's own risk
    charter, and decide whether it may proceed to the approval card.

    You are the last soft checkpoint before the human sees a CONFIRM card. The
    plan has ALREADY passed the deterministic execution caps (slippage,
    notional, price-impact, token-security shield, on-chain simulation). Your
    job is ONLY to enforce the user's charter on top of those.

    Hard rules for your own behaviour:
    - You may only make the decision STRICTER. Never approve, loosen, or excuse
      anything; if the charter is silent on a risk, that is not a violation.
    - If `charter` is empty or whitespace, you are in ADVISORY mode: verdict is
      always "ok", and `summary` is one short informational line about the
      trade's risk (e.g. its size vs the portfolio). Never "blocked" with no
      charter.
    - If `charter` is set, check the trade against every rule in it. If any rule
      is broken, verdict is "blocked": `blocked_reason` must name the exact rule
      and give one concrete compliant fix (e.g. "trim to $10 to meet your $10
      per-trade cap"). Otherwise verdict is "ok".
    - Use `portfolio_context` to resolve "% of portfolio" rules. If it is
      "unknown", say so plainly and do not fabricate a percentage; a rule that
      needs a number you don't have is not proven broken.
    - `summary` is always one short line the user sees on the card.
    """

    charter: str = dspy.InputField(desc="The user's risk rules; empty means advisory-only")
    trade_summary: str = dspy.InputField(desc="The quoted plan: tokens, USD notional, price impact, slippage, verified flag, warnings")
    portfolio_context: str = dspy.InputField(desc="Total wallet USD for %-of-portfolio rules, or 'unknown'")
    verdict: str = dspy.OutputField(desc='Exactly "ok" or "blocked"')
    summary: str = dspy.OutputField(desc="One short line shown on the card")
    blocked_reason: str | None = dspy.OutputField(desc="When blocked: the rule broken + one compliant fix")


class MarketResearch(dspy.Signature):
    """Market Research analyst on a trading desk. Given real fetched market data
    for one asset, write a tight thesis. READ-ONLY: never size, draft, or place
    a trade.

    Return, in order and clearly labelled:
    1. Structure: the trend and the price levels that matter now.
    2. Positioning: funding / open interest / crowdedness -- only if the data
       has it.
    3. Flow: holders / smart-money / concentration -- only if the data has it.
    4. Catalyst: the next scheduled event most likely to move it, if known.
    Finish with two lines: "CONVICTION x/10" and "WRONG IF <one falsifiable
    condition>". Cite only figures present in `market_data`; if a section's data
    is missing, say so in a few words rather than inventing anything.
    """

    asset: str = dspy.InputField()
    market_data: str = dspy.InputField(desc="Real fetched data for the asset (may be partial)")
    thesis: str = dspy.OutputField(desc="Structure / Positioning / Flow / Catalyst + CONVICTION + WRONG IF")
    conviction: int = dspy.OutputField(desc="Integer 1-10")


class TeamSynthesis(dspy.Signature):
    """Coordinator of a trading desk. Fold the specialists' outputs into ONE
    clear answer for the user.

    Do not re-derive numbers -- attribute each to the specialist that produced
    it. Show the desk's voices concisely: the Market Research thesis (with its
    conviction), and for a trade the Execution draft and the Risk verdict. End
    with exactly one recommendation. Never claim a trade was placed; when a
    reviewable card exists the user still approves it separately. If Risk BLOCKED
    the trade, lead with that and the compliant fix -- there is no card.
    """

    request: str = dspy.InputField()
    market_research: str = dspy.InputField()
    execution: str = dspy.InputField(desc="Execution draft/answer, or 'n/a' for analysis-only")
    risk: str = dspy.InputField(desc="Risk verdict + summary, or 'n/a'")
    answer: str = dspy.OutputField(desc="One synthesized desk answer for the user")


class TokenDeepDive(dspy.Signature):
    """Produce a rigorous, structured deep-dive on ONE token, strictly from the
    supplied evidence bundle. This is the "analysis lens": a due-diligence verdict,
    not a price quote and not a trade instruction.

    Work through only the dimensions the question needs, using this framework where
    the evidence supports it (skip a dimension when its evidence is unavailable, and
    say so briefly if it materially limits the conclusion):
      1. Identity & tokenomics — name/symbol, verification, supply/FDV, program.
      2. Contract safety — mint/freeze authority, honeypot/tax, owner privileges.
         Treat MISSING safety signals as unknown, never as 'safe'.
      3. Liquidity & market — price, market cap, 24h volume, liquidity depth/venues.
      4. Holder structure — top-10 concentration (>~30% = elevated risk), noting
         that holder lists include pools/exchanges/treasury, not beneficial owners.
      5. Events & unlocks — upcoming unlocks are the most deterministic near-term
         headwind; if unavailable, flag the gap.
      6. Sentiment & market phase — global Fear & Greed as context only.
      7. Protocol fundamentals — for a governance/DeFi token, TVL/fees if present.

    Follow `analysis_rules` verbatim -- every "do not confuse" rule is binding.
    Tailor the risk framing to `user_context` (risk tolerance/charter) when given;
    with none, order conservatively and never infer risk appetite from silence.

    Output: a concise structured verdict. Lead with a one-line bottom line and a
    CONFIDENCE LEVEL (high/medium/low) -- never a numeric point score. Use a small
    table for the dimension read when it helps. Cite decision-driving numbers to
    their source dimension. Explicitly name any material missing dimension and its
    effect on confidence. End with EXACTLY ONE line: "What would flip this: <the
    single variable that would most change the verdict>." Never present an estimate
    as a fact; never claim a trade; this is information, not financial advice.
    """

    request: str = dspy.InputField(desc="The user's question about the token")
    evidence: str = dspy.InputField(desc="The composed evidence bundle with per-dimension coverage/source")
    analysis_rules: str = dspy.InputField(desc="Binding grounding rules; obey exactly")
    user_context: str = dspy.InputField(desc="User risk charter/preferences, or 'no profile set'")
    learned_lessons: str = dspy.InputField(
        desc="Prior process lessons for this asset (advisory only; never override safety), or 'none'")
    answer: str = dspy.OutputField(desc="Structured due-diligence verdict with confidence + flip-variable")


class RoleReflection(dspy.Signature):
    """Reflect on a past analysis decision WITHOUT outcome bias, to produce one
    concise process correction (a lesson) for future analyses of this asset.

    Judge the DECISION PROCESS given the evidence available at decision time, not
    the outcome itself: a correct process can lose and a flawed process can win, so
    never say "the call was right/wrong because price moved". Use the realized move
    only as post-hoc evidence about what the decision under-weighted or over-weighted.

    Anchor on these failure modes: using unavailable data as fact; ignoring the
    stated invalidation variable; confusing a forecast with an execution instruction;
    sizing from conviction instead of risk.

    Output ONE sentence: a durable, general process correction (not asset-price
    prediction). It is ADVISORY guidance for future reasoning only — it never
    authorizes a trade, never loosens a risk limit, and is always overridden by the
    static safety rules and the human confirmation gate.
    """

    thesis: str = dspy.InputField(desc="The original one-line verdict")
    flip_variable: str = dspy.InputField(desc="The invalidation variable named at decision time")
    realized_move: str = dspy.InputField(desc="Realized price move since the decision, e.g. '-18% over 6 days'")
    lesson: str = dspy.OutputField(desc="One concise, general process correction")


general_agent = dspy.Predict(GeneralAnswer)
equity_research_synthesizer = dspy.Predict(EquityResearchAnswer)
risk_agent = dspy.Predict(RiskAssessment)
market_research_agent = dspy.Predict(MarketResearch)
team_coordinator = dspy.Predict(TeamSynthesis)
token_deepdive_agent = dspy.Predict(TokenDeepDive)
role_reflection_agent = dspy.Predict(RoleReflection)
_mcp_registry = get_mcp_registry()
_llm_slots = asyncio.Semaphore(max(1, settings.max_concurrent_llm_requests))


def _run_program(program, lm, kwargs):
    # Select the LM for this call inside the worker thread, so a fallback swap is
    # scoped to this invocation and never mutates the global config other
    # concurrent turns are using.
    with dspy.context(lm=lm):
        return program(**kwargs)


async def _call_lm(program, **kwargs):
    """Apply shared model-provider backpressure to every DSPy invocation, a fresh
    repeat-call guard scope (app/repeat_guard.py) for this program's own tool loop
    (a no-op for a program with no tools), and LLM-hop resilience: an explicit
    per-request timeout and litellm retries on the primary model, then one attempt
    on the configured fallback model when the primary fails with a transient
    provider error (outage, rate limit, 5xx). A non-transient error -- bad request,
    auth, context length -- is identical on any model, so it surfaces immediately."""
    token = start_guard()
    try:
        async with _llm_slots:
            increment("llm_calls")
            try:
                return await asyncio.to_thread(_run_program, program, _primary_lm, kwargs)
            except Exception as exc:
                if _fallback_lm is None or not _is_transient_lm_error(exc):
                    raise
                increment("llm_primary_transient_failures")
                logger.warning(
                    "LM primary '%s' failed transiently (%s); retrying on fallback '%s'",
                    settings.model, type(exc).__name__, settings.llm_fallback_model,
                )
                try:
                    result = await asyncio.to_thread(_run_program, program, _fallback_lm, kwargs)
                except Exception:
                    increment("llm_fallback_failures")
                    raise
                increment("llm_fallback_used")
                return result
    finally:
        reset_guard(token)


portfolio_agent = dspy.ReAct(
    PortfolioAnswer, tools=guard_tools([portfolio_snapshot, sol_balance, spl_balances]), max_iters=4
)
trade_planner = dspy.ReAct(
    TradeProposalExtraction,
    tools=guard_tools([sol_balance, spl_balances, search_verified_tokens, token_safety_warnings]),
    max_iters=6,
)
trade_simulator = dspy.ReAct(
    TradeSimulationExtraction,
    tools=guard_tools([sol_balance, spl_balances, search_verified_tokens]),
    max_iters=6,
)
