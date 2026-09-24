"""Shared bounded model runtime and prompt definitions; no workflow decisions."""
import asyncio
import logging
import dspy
from dspy.streaming import StatusMessage, StatusMessageProvider, StreamListener, StreamResponse
from app import streaming
from app.agent import portfolio_snapshot, search_verified_tokens, sol_balance, spl_balances, token_safety_warnings
from app.mcp_tools import get_mcp_registry
from app.metrics import increment
from app.repeat_guard import guard_tools, reset_guard, start_guard
from app.settings import settings

logger = logging.getLogger(__name__)

# Every DSPy hop gets an explicit per-request timeout (litellm otherwise waits up
# to 600s on a wedged provider) plus litellm's own exponential-backoff retries
# against the same model. See _call_lm for the cross-provider fallback on top.
def _reasoning_kwargs(model: str | None) -> dict:
    """A reasoning model (GPT-5.x, o-series) takes no sampling temperature and
    needs room for its reasoning tokens; the effort level is passed through
    when configured."""
    name = (model or "").lower()
    if not any(tag in name for tag in ("gpt-5", "/o1", "/o3", "/o4", "o1-", "o3-", "o4-")):
        return {}
    out = {"temperature": 1.0, "max_tokens": 16000}
    if settings.research_reasoning_effort:
        out["reasoning_effort"] = settings.research_reasoning_effort
    return out



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
_intent_lm = (
    dspy.LM(
        settings.intent_model,
        timeout=settings.llm_request_timeout_seconds,
        num_retries=settings.llm_num_retries,
    )
    if settings.intent_model and settings.intent_model != settings.model
    else None
)
_synthesis_lm = (
    dspy.LM(
        settings.synthesis_model,
        timeout=settings.llm_request_timeout_seconds,
        num_retries=settings.llm_num_retries,
    )
    if settings.synthesis_model and settings.synthesis_model != settings.model
    else None
)
_planner_model = settings.planner_model or settings.research_model     # the planner runs on the research tier unless it has its own model
_planner_lm = (
    dspy.LM(_planner_model, timeout=settings.llm_request_timeout_seconds, num_retries=settings.llm_num_retries, **_reasoning_kwargs(_planner_model))
    if _planner_model and _planner_model != settings.model
    else None
)
_research_lm = (
    dspy.LM(settings.research_model, timeout=settings.llm_request_timeout_seconds, num_retries=settings.llm_num_retries, **_reasoning_kwargs(settings.research_model))
    if settings.research_model and settings.research_model != settings.model
    else None
)
# The same research model at the loop's effort for its structured calls.
_research_loop_lm = (
    dspy.LM(settings.research_model, timeout=settings.llm_request_timeout_seconds, num_retries=settings.llm_num_retries,
            **{**_reasoning_kwargs(settings.research_model), **({"reasoning_effort": settings.research_loop_effort} if settings.research_loop_effort and _reasoning_kwargs(settings.research_model) else {})})
    if settings.research_model and settings.research_model != settings.model
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

    You are Orbit, a copilot for markets and assets: crypto across chains
    (Solana, Ethereum, Base, Arbitrum and others) and equities alike -- market
    data, tokens, stocks and other assets, wallets and portfolios, news and
    why something is moving, on-chain activity, and swaps. Use this for
    greetings, small talk, and conceptual questions about those topics or this
    assistant's capabilities. A greeting should offer help with markets, assets
    (crypto or equities) or trading activity in general -- never frame the
    assistant as Solana-only or crypto-only.
    Never state a live balance, price, or trade status here — those require the
    research/portfolio/trade intents instead.
    """

    request: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    answer: str = dspy.OutputField()


class CompositeSynthesis(dspy.Signature):
    """Read several evidence cards together and write a short summary (3-6
    sentences) of what they say IN COMBINATION: what agrees, what conflicts,
    what stands out. Use only facts present in the evidence and name the card
    each fact comes from ("per the gainers list", "the sentiment snapshot").
    Never add a number, token or claim that is not in the evidence. When the
    stance is a market read, do not tell the user what to buy; describe the
    conditions the evidence shows and the risks it shows. Verification,
    Shield or organic-score checks are NOT an audit: never write that a
    token is audited unless a card names an audit report or auditing firm;
    say "no audit report in the evidence" instead. A property no card
    lists is unknown: never write that an extension, feature, authority,
    label or audit is absent, supported or "not reported" unless a card
    says so; say "not in the evidence". A request that asks to compare
    dates when the cards are current data only describes the current data
    as current, never as a change or as "no change" between those dates.
    If a card asks the user a question instead of answering, repeat the
    question; never pick a subject the question did not name. When the request states a word
    limit, or opens with "What does this mean for the market:" (a headline
    tap), answer in under 150 words, quote figures and dates exactly as the
    source states them with the source named, and give no sector or stock
    picks. Keep every figure's unit and meaning:
    a "24h price change" column is a PRICE move, never volume growth; dollar
    liquidity, volume or market cap are totals, never a price level; a value
    shown as $0.0000 is a rounded small price, not a near-zero price."""

    request: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="The cards, separated by ---; each is verbatim tool output")
    stance: str = dspy.InputField(desc="'a factual summary' or 'a market read, not a recommendation'")
    summary: str = dspy.OutputField()


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

    Evidence discipline (UI review, 2026-09-23): when the data contradicts the
    question's premise ("why is SOL down" while the card shows +1.5%), say so
    first and answer the true state. Never state a launch or creation date
    without a source that dates it; a first-buyer or first-trade time is not a
    launch date. A token program (Token-2022) does not imply any extension;
    name only extensions the data lists. "Secure", "safe", "unanimous" or
    "bullish crowd" need the evidence that says so, with its sample size;
    otherwise describe what was checked and what was not. Honour the user's
    stated constraints (a horizon, "without another volatile token", one
    chain): when nothing matches, say no match rather than a near miss.
    Claims about who controls a token's supply are bounded by what was
    examined: a bundle check of two early buyers says nothing about
    "distributed control", and the top token accounts are accounts (pools,
    exchanges, program addresses), never "beneficial-owner concentration"
    unless owners were resolved; say "N accounts examined, owners not
    resolved". A Solana-only check (Jupiter Shield, Jupiter verification)
    does not make an Ethereum contract "unverified"; name the check that
    applies instead. Never call a wallet "diversified" or a concentration
    "not risky": report the numbers and what they depend on.
    """

    request: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    answer: str = dspy.OutputField(desc="Concise grounded response including risks")


class KnowledgeAnswer(dspy.Signature):
    """Answer a protocol / concept question strictly from numbered knowledge-base
    passages. Cite every factual claim as [n]. If the passages don't cover part
    of the question, say exactly what is missing instead of filling it in. If
    they do not answer the question at all (the question asks about investors,
    a hack, a vote or a figure and no passage mentions it), reply with exactly
    one line: `NOT COVERED: <what the passages lack>` -- nothing else, no
    Sources -- so the answer can come from a source that has it.
    Prefer the passage that matches the protocol version the user named; when
    passages describe different versions (e.g. v3 vs v4), say which is which.
    Write for a trader: concrete parameters, mechanics, and what they imply.
    Describe backing as the passages state it: "fully backed by delta-neutral
    positions" is not "over-collateralized" or "assets worth more than the
    supply". A question about what happened "recently" or "latest" is
    answered only from passages that carry a date; give that date, and say
    that the knowledge base may lag newer events. No investment advice."""

    request: str = dspy.InputField()
    conversation_history: str = dspy.InputField(desc="Prior turns, oldest first; empty if none")
    passages: str = dspy.InputField(desc="Numbered passages [n] with protocol, document and heading")
    answer: str = dspy.OutputField(desc="Cited answer in Markdown; ends with a Sources list of the [n] used")


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
    Never propose a trade here. Never judge suitability: no "reasonably
    diversified", "healthy" or "not risky" -- two assets at 81/19 is a
    concentration fact, and whether it is a risk depends on the assets and
    on exit depth, which the snapshot does not measure.
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
    Units are not interchangeable: liquidity, volume and market cap are dollar
    TOTALS and are never a support, resistance or invalidation PRICE. A price
    level may be named only when that price figure is in `market_data`;
    otherwise write "no level in the data". Name a catalyst only when the data
    dates an event; "possible announcements" is not a catalyst. Name the asset
    as the data identifies it (chain, contract) and say when the identity is
    unverified; then conviction cannot exceed 4/10.
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
    # The typed view behind the prose: one word each, so the verdict can be
    # folded into a Signal and blended, compared and replayed without parsing
    # the answer text. Neutral when the evidence is mixed or too thin.
    stance: str = dspy.OutputField(desc="Exactly one of: bullish, bearish, neutral -- the direction of the bottom line")
    confidence: str = dspy.OutputField(desc="Exactly one of: high, medium, low -- the same confidence level the answer states")


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
knowledge_synthesizer = dspy.Predict(KnowledgeAnswer)
risk_agent = dspy.Predict(RiskAssessment)
market_research_agent = dspy.Predict(MarketResearch)
team_coordinator = dspy.Predict(TeamSynthesis)
token_deepdive_agent = dspy.Predict(TokenDeepDive)
composite_synthesizer = dspy.Predict(CompositeSynthesis)
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
    async with _llm_slots:
        increment("llm_calls")
        try:
            return await _run_guarded(program, _primary_lm, kwargs)
        except Exception as exc:
            if _fallback_lm is None or not _is_transient_lm_error(exc):
                raise
            increment("llm_primary_transient_failures")
            logger.warning(
                "LM primary '%s' failed transiently (%s); retrying on fallback '%s'",
                settings.model, type(exc).__name__, settings.llm_fallback_model,
            )
            try:
                result = await _run_guarded(program, _fallback_lm, kwargs)
            except Exception:
                increment("llm_fallback_failures")
                raise
            increment("llm_fallback_used")
            return result


# When set, every synthesis call is reported here as (program name, kwargs)
# before it runs: scripts/synthesis_eval/capture.py records the evidence
# bundles the synthesis tier is given, so different models can be scored on
# identical evidence. None in normal operation.
synthesis_recorder = None


class _ToolStatus(StatusMessageProvider):
    """What a program's tool loop reports to the client: the tool it is about
    to run, nothing else (DSPy's defaults narrate every LM call too)."""

    def tool_start_status_message(self, instance, inputs):
        return f"Running {str(getattr(instance, 'name', 'tool')).replace('_', ' ')}"

    def tool_end_status_message(self, outputs):
        return None


async def _stream(program, field: str, on_delta, lm, kwargs):
    """Run `program` on `lm` with output `field` handed to `on_delta` token by
    token and each tool call reported as a status line. The final prediction,
    or None when the stream produced none (the caller then makes the ordinary
    call). Same backpressure and repeat-guard scope as _run_guarded."""
    streamer = dspy.streamify(program, stream_listeners=[StreamListener(signature_field_name=field)],
                              status_message_provider=_ToolStatus(), async_streaming=True,
                              include_final_prediction_in_output_stream=True)
    final = None
    async with _llm_slots:
        increment("llm_calls")
        token = start_guard()
        try:
            with dspy.context(lm=lm):
                async for chunk in streamer(**kwargs):
                    if isinstance(chunk, StreamResponse):
                        if chunk.chunk:
                            on_delta(chunk.chunk)
                    elif isinstance(chunk, StatusMessage):
                        streaming.emit("status", text=chunk.message)
                    elif isinstance(chunk, dspy.Prediction):
                        final = chunk
        finally:
            reset_guard(token)
    return final


async def stream_answer(program, field: str, on_delta, **kwargs):
    """Run a program on the primary model with its `field` streamed token by
    token to `on_delta` as it is generated. Returns the final prediction. Any
    failure falls back to the ordinary call, so a streaming client never gets
    less than a non-streaming one."""
    try:
        final = await _stream(program, field, on_delta, _primary_lm, kwargs)
        if final is not None:
            return final
    except Exception:
        logger.warning("streaming answer failed; falling back to a whole answer", exc_info=True)
    return await _call_lm(program, **kwargs)


async def stream_synthesis(program, field: str, on_delta, research: bool = False, **kwargs):
    """stream_answer on the synthesis tier's model (the research tier's when
    `research` and one is configured)."""
    if synthesis_recorder is not None:
        synthesis_recorder(_program_name(program), kwargs)
    try:
        final = await _stream(program, field, on_delta, (_research_lm if research else None) or _synthesis_lm or _primary_lm, kwargs)
        if final is not None:
            return final
    except Exception:
        logger.warning("streaming synthesis failed; falling back to a whole answer", exc_info=True)
    return await _call_synthesis_lm(program, **kwargs)


async def answer(program, field: str = "answer", *, tier: str = "primary", **kwargs):
    """The call a node makes for the text the user reads. When a client is
    streaming this turn (app/streaming.py) the `field` reaches it token by
    token and every tool the program runs is reported as a status line;
    otherwise this is the ordinary bounded call on the named tier. Every
    path that ends in model-written text goes through here, so a general
    reply, a single-tool answer, a ReAct research turn and a deep dive all
    stream the same way a composed answer does."""
    if streaming.active():
        stream = stream_synthesis if tier == "synthesis" else stream_answer
        return await stream(program, field, lambda text: streaming.emit("delta", text=text), **kwargs)
    if tier == "synthesis":
        return await _call_synthesis_lm(program, **kwargs)
    return await _call_lm(program, **kwargs)


def planner_available() -> bool:
    """Whether a model can write question contracts: the planner model, else
    the primary one. Tests and keyless deployments use the rules planner."""
    key = settings.openai_api_key if hasattr(settings, "openai_api_key") else None
    return bool(settings.planner_model) or bool(key and not str(key).startswith("<") and "placeholder" not in str(key).lower())


async def _call_planner_lm(program, **kwargs):
    """The planner tier: `planner_model` when configured, else the primary
    path; a transient failure on the planner model falls back to the primary."""
    if _planner_lm is None:
        return await _call_lm(program, **kwargs)
    async with _llm_slots:
        increment("llm_calls")
        try:
            return await _run_guarded(program, _planner_lm, kwargs)
        except Exception as exc:
            if not _is_transient_lm_error(exc):
                raise
            logger.warning("planner model '%s' failed transiently (%s); using the primary path", settings.planner_model, type(exc).__name__)
    return await _call_lm(program, **kwargs)


async def _call_synthesis_lm(program, **kwargs):
    """The synthesis tier: `synthesis_model` when configured, else the primary
    path. Same backpressure and guard scope as _call_lm; a transient failure
    on the synthesis model falls back to the primary path rather than the
    fallback model, so a domain model under trial can never take an answer
    down with it."""
    if synthesis_recorder is not None:
        synthesis_recorder(_program_name(program), kwargs)
    if _synthesis_lm is None:
        return await _call_lm(program, **kwargs)
    async with _llm_slots:
        increment("llm_calls")
        try:
            return await _run_guarded(program, _synthesis_lm, kwargs)
        except Exception as exc:
            if not _is_transient_lm_error(exc):
                raise
            increment("llm_synthesis_transient_failures")
            logger.warning("synthesis model '%s' failed transiently (%s); using the primary path", settings.synthesis_model, type(exc).__name__)
    return await _call_lm(program, **kwargs)


async def _call_research_loop_lm(program, **kwargs):
    """The loop's structured calls: the research model at `research_loop_effort`."""
    if _research_loop_lm is None:
        return await _call_research_lm(program, **kwargs)
    async with _llm_slots:
        increment("llm_calls")
        try:
            return await _run_guarded(program, _research_loop_lm, kwargs)
        except Exception as exc:
            if not _is_transient_lm_error(exc):
                raise
            logger.warning("research loop model failed transiently (%s); using the research path", type(exc).__name__)
    return await _call_research_lm(program, **kwargs)


async def _call_research_lm(program, **kwargs):
    """The research tier: `research_model` when configured, else the synthesis
    tier's path. A transient failure falls back the same way."""
    if _research_lm is None:
        return await _call_synthesis_lm(program, **kwargs)
    async with _llm_slots:
        increment("llm_calls")
        try:
            return await _run_guarded(program, _research_lm, kwargs)
        except Exception as exc:
            if not _is_transient_lm_error(exc):
                raise
            increment("llm_research_transient_failures")
            logger.warning("research model '%s' failed transiently (%s); using the synthesis path", settings.research_model, type(exc).__name__)
    return await _call_synthesis_lm(program, **kwargs)


def _program_name(program) -> str:
    for name, value in globals().items():
        if value is program:
            return name
    return type(program).__name__


async def _call_intent_lm(program, **kwargs):
    """The routing classifier on its own (faster) model when one is configured;
    otherwise identical to _call_lm, including its resilience and fallback."""
    if _intent_lm is None:
        return await _call_lm(program, **kwargs)
    async with _llm_slots:
        increment("llm_calls")
        try:
            return await _run_guarded(program, _intent_lm, kwargs)
        except Exception as exc:
            if not _is_transient_lm_error(exc):
                raise
            increment("llm_intent_transient_failures")
            return await _run_guarded(program, _primary_lm, kwargs)


async def _run_guarded(program, lm, kwargs):
    # One guard scope per model attempt: the fallback run must not inherit the
    # aborted primary run's seen-calls set, or its first legitimate call to the
    # same (tool, args) is rejected as a repeat.
    token = start_guard()
    try:
        return await asyncio.to_thread(_run_program, program, lm, kwargs)
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
