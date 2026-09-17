"""Compiled lexical evidence used by deterministic routing rules."""

import re


CURRENT = re.compile(r"\b(?:latest|breaking|news|current|currently|today|right now|now|recent|live|up[- ]to[- ]date|as of|this week|trending|gainers?|losers?|new pairs?)\b", re.I)
TRADE = re.compile(r"\b(?:swap|buy|sell|exchange|bridge|trade|convert)\b", re.I)
# Checked ahead of TRADE -- deliberately covers past-tense verbs ("sold",
# "bought") that TRADE's word list doesn't match at all, which is exactly
# why a phrasing like "what would happen if I sold half my SOL" used to
# skip TRADE/has_competing_speech entirely and fall through to the generic
# OPEN_QUESTION fallback instead of getting real simulation handling.
SIMULATE_TRADE = re.compile(
    r"\b(?:what\s+would\s+happen\s+if\s+i|what\s+if\s+i|should\s+i|simulate)\b"
    r".{0,40}\b(?:sell(?:ing)?|sold|buy(?:ing)?|bought|swap(?:ping|ped)?|trad(?:e|ing|ed)|exchang(?:e|ing|ed)|convert(?:ing|ed)?)\b",
    re.I,
)
# "Should I buy X?" with no holding or amount is a recommendation question,
# not a simulation of the user's position -- it must reach the speech layer
# (advice -> research) instead of inventing a trade to quote.
ADVICE_QUESTION = re.compile(r"\bshould\s+i\b", re.I)
OWN_POSITION_OR_AMOUNT = re.compile(r"\b(?:my|mine|half|all|some|rest|everything)\b|\d", re.I)
IMPERATIVE_MOVE = re.compile(r"^\s*(?:please\s+)?move\s+(?:all\s+|half\s+|my\s+|\.?\d)", re.I)
TRADE_CANCEL = re.compile(r"^\s*(?:cancel|dismiss|discard|stop|forget)(?:\s+(?:it|this|the|my|pending|current))?(?:\s+(?:swap|trade|quote|order|transaction|route|plan))?\s*[.!]?\s*$|^\s*never\s*mind(?:\s+(?:the|this)\s+(?:swap|trade|quote|order))?\s*[.!]?\s*$", re.I)
TRADE_CONFIRM = re.compile(r"^\s*(?:confirm|approve|proceed|go ahead|execute)(?:\s+with)?(?:\s+(?:this|the|my|pending|current))?(?:\s+(?:swap|trade|quote|order|transaction|route|plan))?(?:\s+[A-Za-z0-9_-]{6,128})?\s*[.!]?\s*$", re.I)
TRADE_MODIFIER = re.compile(r"^\s*(?:(?:use|set|change|update|make)(?:\s+(?:the|it|amount|slippage|recipient|destination|source))?|with|max(?:imum)?|instead|send\s+to)\b", re.I)
# Risk-charter commands (Minara-style "Custom Prompt" for risk rules). SET
# captures the rules text in group 1; SHOW/CLEAR are whole-message controls.
CHARTER_SET = re.compile(
    r"^\s*(?:set|update|change|save|define)\s+(?:my\s+)?risk\s+(?:charter|rules|profile)\s*[:=\-]?\s*(?P<rules>.+)$"
    r"|^\s*my\s+risk\s+(?:charter|rules|profile)\s+(?:is|are)\s*[:=\-]?\s*(?P<rules2>.+)$",
    re.I | re.S,
)
CHARTER_SHOW = re.compile(r"^\s*(?:show|view|display|what(?:'s| is| are)?)\s+(?:my\s+)?(?:current\s+)?risk\s+(?:charter|rules|profile)\s*\??\s*$", re.I)
CHARTER_CLEAR = re.compile(r"^\s*(?:clear|remove|delete|reset|forget|drop)\s+(?:my\s+)?risk\s+(?:charter|rules|profile)\s*\.?\s*$", re.I)
# Team-mode ("trading desk") session toggle -- the multi-agent room.
TEAM_ENABLE = re.compile(r"^\s*(?:enable|turn\s+on|activate|start|open|use)\s+(?:the\s+)?(?:team|trading\s+desk|desk)(?:\s+mode|\s+room)?\s*\.?\s*$", re.I)
TEAM_DISABLE = re.compile(r"^\s*(?:disable|turn\s+off|deactivate|stop|close|exit|leave)\s+(?:the\s+)?(?:team|trading\s+desk|desk)(?:\s+mode|\s+room)?\s*\.?\s*$", re.I)
TEAM_STATUS = re.compile(r"^\s*(?:team|desk)\s+(?:mode\s+)?status\s*\??\s*$|^\s*is\s+team\s+mode\s+(?:on|off|enabled|active)\s*\??\s*$", re.I)
SOL_PAYMENT = re.compile(r"(?<![\w.])(?:\d+(?:\.\d+)?|\.\d+)\s*SOL\b", re.I)
SOL_SOURCE = re.compile(r"\b(?:swap|buy|sell|exchange|bridge|trade|convert|move)\s+(?:all|half|max(?:imum)?)\s+(?:my\s+|the\s+)?SOL\b", re.I)
CROSS_CHAIN = re.compile(r"\b(?:cross[- ]chain|bridge)\b|\b(?:from|on)\s+(?:base|robinhood(?:\s+chain)?|ethereum|arbitrum|optimism|bnb|polygon|avalanche)\b.{0,80}\b(?:to|into|on)\s+(?:solana|base|robinhood(?:\s+chain)?|ethereum|arbitrum|optimism|bnb|polygon|avalanche)\b", re.I)
OWN_WALLET = re.compile(r"\b(?:my|this|the|connected)\b.{0,30}\b(?:wallet|portfolio|balance|balances|holdings)\b", re.I)
OWN_TOKEN_BALANCE = re.compile(r"\bhow\s+much(?:\s+of)?\s+(?:\$?[A-Za-z][A-Za-z0-9._-]{1,15}|this\s+(?:token|coin))\s+(?:do\s+)?i\s+have\b|\b(?:my|connected\s+wallet'?s?)\s+(?:\$?[A-Za-z][A-Za-z0-9._-]{1,15}|this\s+(?:token|coin))\s+balance\b|\bbalance\s+of\s+(?:my\s+)?(?:\$?[A-Za-z][A-Za-z0-9._-]{1,15}|this\s+(?:token|coin))\b|\bdo\s+i\s+have(?:\s+any)?\s+(?:\$?[A-Za-z][A-Za-z0-9._-]{1,15}|this\s+(?:token|coin))\b", re.I)
OWN_TOKEN_HOLDINGS = re.compile(r"\b(?:my\s+|connected\s+wallet(?:'s)?\s+)?(?:spl|erc-?20)?\s*token\s+holdings?\b|\b(?:show|list|view|check)\s+(?:the\s+)?(?:spl|erc-?20)\s+(?:tokens?|holdings?)\b|\b(?:assets?|tokens?)\s+in\s+(?:my|the\s+connected)\s+wallet\b", re.I)
OWN_WALLET_ACTIVITY = re.compile(
    # "my/this/the/connected wallet('s) recent transactions" -- "this wallet"
    # is what the app's own portfolio-card quick action emits, and referred to
    # the connected wallet; without it the request fell through to web search.
    r"\b(?:show|list|check|view)?\s*(?:my|this|the|connected)\s+wallet(?:'s)?\s+(?:recent\s+|latest\s+)?(?:transactions?|activity|history|transfers?)\b"
    r"|\b(?:show|list|check|view)?\s*my\s+(?:recent\s+|latest\s+)?(?:transactions?|activity|history|transfers?)\b"
    r"|\b(?:recent|latest)\s+(?:transactions?|activity|transfers?)\s+(?:in|for|from)\s+(?:my|this|the\s+connected|the)\s+wallet\b",
    re.I,
)
WALLET_HEALTH = re.compile(r"\b(?:my|connected)\b.{0,30}\b(?:wallet health|wallet safety|approvals?|gas readiness)\b", re.I)
PORTFOLIO_SCENARIO = re.compile(r"\b(?:what if|scenario|stress test|drops?|falls?|rises?|increases?)\b.{0,80}\b(?:portfolio|wallet|SOL|token|market)\b|\b(?:portfolio|wallet|SOL|token|market)\b.{0,80}\b(?:drops?|falls?|rises?|increases?)\b", re.I)
WALLET = re.compile(r"\b(?:wallet|address|portfolio|holdings?|holds?|holding|balances?|pnl|transactions?|leverage|debt|liquidation|counterparties)\b", re.I)
# A perpetual-positions ask about a wallet: "open perps for 0x...", "my open
# perps", "hyperliquid positions for <address>". Positions are a wallet's
# state, so this is a portfolio ask whichever wallet it names; market-data
# perp vocabulary ("funding rate for BTC") has no wallet or "my" in it.
PERP_POSITIONS = re.compile(
    r"\b(?:open\s+)?(?:perps?|perpetuals?|perp\s+positions?|hyperliquid\s+positions?|positions?\s+on\s+hyperliquid)\b"
    r".{0,40}\b(?:for|of|on|held\s+by|is|does)\b|\b(?:my|our)\s+(?:open\s+)?(?:perps?|perpetuals?|perp\s+positions?|hyperliquid\s+positions?)\b"
    r"|\bwhat\s+perps?\s+(?:is|does|are)\b",
    re.I,
)
WALLET_OWNER = re.compile(r"\b(?:wallet|portfolio|balances?|pnl|transactions?|counterparties)\b", re.I)
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}|(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")
TOKEN = re.compile(r"\b(?:tokens?|coins?|mints?|contracts?|memecoins?|meme\s+coins?|holders?|liquidity)\b", re.I)
SECURITY = re.compile(r"\b(?:safe(?:ty)?|secur(?:e|ity)|risky?|rugs?(?:\s?pulls?)?|scams?|honey\s?pots?|sellab\w*|audits?)\b", re.I)
# Crypto-specific security jargon (or a $TICKER paired with a security word) is
# unambiguous even without a "token/coin/contract" word nearby. Plain "safe" or
# "risk" alone stay excluded here since they are common in non-crypto questions.
SECURITY_STRONG = re.compile(
    r"\brug(?:ged|ging|s|\s?pull(?:ed|s)?)?\b"
    r"|\bhoney\s?pot(?:s|ted)?\b"
    r"|\bsellability\b"
    # "can I sell X" / "is it sellable": a honeypot question, not a sell order.
    r"|\bcan\s+(?:i|you|we|one|anyone)\s+(?:even\s+|still\s+|actually\s+)?sell\b|\b(?:un)?sellable\b"
    r"|\$[A-Za-z][A-Za-z0-9]{1,9}\b[^.\n?!]{0,24}\b(?:scam|safe|safety|legit|rug|honeypot)\b"
    r"|\b(?:scam|safe|safety|legit)\b[^.\n?!]{0,24}\$[A-Za-z][A-Za-z0-9]{1,9}\b",
    re.I,
)
MARKET = re.compile(
    r"\b(?:price|market|volume|ohlcv|chart|candles?|dex|trending|gainers?|losers?|new pairs?|new pools?|token profiles?|"
    r"launches?|trades?|buys?|sells?|flow|gems?|memecoins?|"
    # Hyperliquid / perp market-data vocabulary: "funding rate for BTC on
    # Hyperliquid" carries no generic market word, so without these it
    # classified as web_research and never reached goldrush_hyperliquid_market.
    r"funding\s*rate|perps?|perpetuals?|hyperliquid|open\s*interest|mark\s*price|oracle\s*price)\b"
    # Launchpad names are discovery scopes ("hottest gems on pump.fun"): without
    # these, slangy phrasings fell to the embedding tier and abstained to
    # general instead of reaching the discovery/pools tools.
    r"|\b(?:pump\.?\s?fun|pumpfun|letsbonk|bonk\.?fun|moonshot)\b",
    re.I,
)
# Checked ahead of the generic MARKET rule below, since "market sentiment"
# contains the bare word "market" and would otherwise be swallowed by it.
SENTIMENT = re.compile(
    r"\bfear\s*(?:and|&|/)\s*greed\b|\bgreed\s+index\b|\baltcoin\s*season\b|\balt\s*season\b"
    r"|\b(?:market|crypto)\s+sentiment\b|\bsentiment\s+index\b",
    re.I,
)
URL = re.compile(r"https?://[^\s<>]+", re.I)
# "who is X" alone is too broad (matches "who is winning", "who is right");
# require it to name a role/title, a "founder/team of Y" shape, or another
# people-search phrase so plain trivia questions fall through to research.
PEOPLE = re.compile(
    r"\bpeople search\b"
    r"|\bfind (?:a |an |the |some )?(?:person|people|professional|employee|founder|co-?founder|engineer|developer|executive|team)s?\b"
    r"|\b(?:find|research|look up|who(?:'s| is| are)) (?:the |a |an )?(?:founder|co-?founder|ceo|cto|cfo|coo|president|chair(?:man|person|woman)?|head|director|creator|owner|team|inventor)s? (?:of|behind|at|for)\b"
    r"|\bwho (?:is|are) (?:the |a |an )?(?:founder|co-?founder|ceo|cto|cfo|coo|president|chair(?:man|person|woman)?|head of|director|creator|owner|inventor)\b"
    r"|\bemployees? at\b|\bworks? at\b|\bprofessional profile\b|\blinked ?in(?: profile)?\b",
    re.I,
)
WEB3_PROJECT = re.compile(r"\b(?:search|find|lookup|research|analy[sz]e|show)\b.{0,40}\b(?:crypto|web3)?\s*projects?\b", re.I)
WEB3_VC = re.compile(r"\b(?:search|find|lookup|research|analy[sz]e|show)\b.{0,40}\b(?:crypto|web3)?\s*(?:vcs?|venture capital|investors?|funds?)\b", re.I)
WEB3_PEOPLE = re.compile(r"\b(?:search|find|lookup|research|who is)\b.{0,50}\b(?:crypto|web3)\b.{0,30}\b(?:person|people|founder)?\b|\b(?:crypto|web3)\b.{0,30}\b(?:person|people|founder)\b", re.I)
FINANCE = re.compile(r"\b(?:stock|stocks|share price|ticker|equity|earnings|revenue|market cap|p/?e|financials?|analyst rating|dividend|forex|commodit(?:y|ies)|bitcoin price|btc price|ethereum price|eth price)\b", re.I)
EQUITY = re.compile(r"\b(?:equity research|stock market|stocks?|shares?|equities|earnings|dividend|analyst ratings?|price target|fundamental analysis|nse|bse|nifty|sensex|nasdaq|nyse|s&p\s*500|dow jones|us market|indian? market)\b", re.I)
EQUITY_TICKER = re.compile(r"(?:\$[A-Z]{1,6}|\b(?:NSE|BSE|NASDAQ|NYSE):[A-Z0-9.-]{1,16})\b", re.I)
DEFI = re.compile(r"\b(?:defi|tvl|total value locked|protocol tvl|chain tvl)\b", re.I)
LISTING = re.compile(r"\b(?:listings?|listed|delistings?|delisted|exchange announcements?)\b", re.I)
CONCEPTUAL = re.compile(r"^(?:hi|hello|hey|thanks|thank you|what can you do)[!?. ]*$", re.I)
# A last-resort deterministic fallback for plain information-seeking questions
# that match none of the specific rules above (e.g. general trivia). Keeps
# "should/would/could" out so genuinely ambiguous advice-shaped requests still
# reach semantic classification instead of being answered flatly.
OPEN_QUESTION = re.compile(
    r"^\s*(?:what'?s?|whom|whose|why|how|when|where|which|explain|"
    r"tell me|describe|define|summar(?:ize|ise)|is there|are there|does|do|did|who)\b",
    re.I,
)
EXECUTION_EXPLANATION = re.compile(r"^\s*(?:how\s+(?:does\s+)?(?:the\s+)?(?:relay(?:\.link)?\s+)?(?:cross[- ]chain\s+)?(?:bridge|bridging|swap)\s+(?:work|works|operate)|what\s+is\s+(?:a\s+|the\s+)?(?:relay(?:\.link)?\s+)?(?:bridge|bridging|cross[- ]chain\s+swap)|explain\s+(?:how\s+)?(?:relay(?:\.link)?\s+)?(?:bridge|bridging|cross[- ]chain\s+swap))\b", re.I)

CHAIN_ALIASES = {
    "solana": "solana", "ethereum": "ethereum", "base": "base",
    "robinhood chain": "robinhood", "robinhood": "robinhood",
    "arbitrum": "arbitrum", "optimism": "optimism", "polygon": "polygon",
    "bnb": "bnb", "bsc": "bnb", "avalanche": "avalanche", "sui": "sui",
    "tron": "tron", "bitcoin": "bitcoin",
}
CHAIN_PATTERN = "|".join(re.escape(alias) for alias in sorted(CHAIN_ALIASES, key=len, reverse=True))
