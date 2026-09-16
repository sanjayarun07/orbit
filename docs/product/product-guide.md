# Product and user guide

[Documentation home](README.md)

## Product purpose

Orbit helps a user investigate a market question, understand a wallet, and prepare a transaction without treating conversation as permission to move funds. The workspace brings together answers, sources, portfolio cards, trade reviews, history, and follow-up tasks.

Its primary users are individual researchers and wallet holders. Paid accounts can also integrate through API keys and MCP. Max accounts can invite members to share a plan and credit pool. Administrators operate the provider catalogue, account support, billing tools, and Knowledge Base.

These are distinct concepts:

- **In-depth analysis / team mode:** several AI roles contribute to an answer.
- **Account team / Members:** people share a subscription and credits. This does not automatically share their private conversations.
- **Public-address connection:** read-only wallet analysis. It is not evidence that a user can sign for that address.
- **Email account sign-in:** identifies the user for history, credits, tasks, and account settings.

## Capability map

| Area | Implemented behavior | Example request | Boundary |
|---|---|---|---|
| Market research | Market overview, narratives, news, token discovery, deep dives | “What are the trending narratives right now?” | Results depend on provider coverage and freshness |
| Token investigation | Identity, prices, holders, trading activity, security indicators | “What are the risks of buying BONK?” | A security indicator is not a guarantee of safety |
| Equity and macro research | Finance/web-backed company research and market-event context | “Research Reliance Industries stock” | No stock order execution or brokerage integration |
| Protocol knowledge | Mechanisms, governance, incidents, investors, funding, relationships | “How does Aave liquidation work?” | Only ingested sources can support corpus-based answers |
| Wallet intelligence | Holdings, balances, activity, health, concentration, scenarios | “What if SOL drops 20%?” | Partial snapshots may leave some holdings unpriced |
| Read-only simulation | Estimate a hypothetical swap against a real quote | “What would I get for half my SOL?” | Simulation is not transaction approval |
| Solana swap preparation | Jupiter quote, checks, simulation, expiring review plan | “Swap 0.01 SOL to USDC on Solana with 50 bps slippage” | Explicit wallet approval and enabled execution are required |
| Cross-chain preparation | Relay draft, quote/review, browser execution, status tracking | “Swap 10 USDC on Base to SOL on Solana” | Supported routes and wallets are provider-dependent |
| Scheduling | Reminders, price alerts, morning briefs, inbox delivery | “Remind me tomorrow at 9am to check SOL” | Tasks notify; they do not execute scheduled trades |
| External-agent access | MCP chat, analytics, data tools, browser handoff | Prepare a swap from an MCP client | The host receives no wallet-signing capability |

## Screen guide

### Home and conversation

Open `/ui/`. The home screen offers a composer, market headlines, sample prompts, and shortcuts for research, portfolio analysis, and swaps. Typing `$` starts token autocomplete. The In-depth switch requests a more involved analysis; trade preferences open the risk-charter editor.

A response can include prose, cited sources, market or portfolio cards, a trade plan, validation notes, and execution status. Sources and structured cards are normal product output. Internal tool traces are controlled separately; raw tool payloads are disabled by default. The presence of an answer does not imply every requested provider succeeded.

The application remembers conversation context such as the connected address, focused asset, pending clarification, trade workflow, and preferences. A reply to a chain clarification can resolve a pending token request. Context is bounded; a conversation is not an unlimited or permanent memory.

### History and sidebar

The sidebar supports new chats, opening recent conversations, rename, pin/unpin, archive/restore, project assignment, link copying, and deletion with inline confirmation.

Titles, pins, archive state, projects, and the recent-chat registry are kept in browser storage. They are not a synchronized server-side project-management feature. Clearing browser storage or switching browsers may remove that organization even when account records still exist.

“Share” copies an account-only conversation URL. Another account is not granted access by receiving it. Archive only hides a chat from Recents; delete removes its retained server history. Message retention is limited to 200 display messages and a two-hour Redis TTL refreshed by writes. A visible sidebar entry is not proof that its transcript still exists.

### Settings

| Tab | Controls |
|---|---|
| Account | Email/account state, wallets, sessions, sign-in/out, plan access |
| Billing | Plan, credit balance, usage, invoices, credit packs, customer portal |
| API keys | Create, choose scopes, inspect, and revoke keys; secret shown once |
| Preferences | Display name, risk preference, default wallet, theme, related preferences |
| Tasks | Create and manage reminders, alerts, and briefs; run, pause, resume, delete |
| Notifications | Low-credit notifications, threshold, receipt and update preferences |
| Data & privacy | Export, delete conversations, delete account with email confirmation |
| Members | Team invitations and member management when entitled |

The interface supports light, dark, and system appearance. Risk preference labels such as “conservative” are distinct from explicit enforceable trade limits.

### Administration

Open `/ui/admin.html` and connect using the deployment's admin key. The shell has four navigation sections:

1. **Accounts & billing:** business metrics, account lookup, plan overrides, credit adjustments, key/session revocation, billing event inspection and replay.
2. **Knowledge base:** a dedicated `/ui/knowledge.html` route for coverage, sources, ingestion state, search, citations, and entity relationships.
3. **Data providers:** tool availability, policies, priorities, quotas, cost estimates, health, and overrides.
4. **Testing:** route/intent previews, provider probes, and specialist investigations including wash-trading runs.

The Knowledge Base page uses the same administration HTML shell; it is a separate workspace route, not a separate service. Public knowledge read endpoints and admin ingestion endpoints have different access boundaries.

## Main user journeys

### Research a topic

1. Ask a question or choose an example.
2. Orbit resolves the request's subject and intent. Ambiguous chains or tokens can trigger a clarification.
3. It selects eligible live tools or Knowledge Base retrieval and builds the answer.
4. Review cited sources, dates, coverage, and warnings. A cached answer may be returned where the router considers reuse suitable.
5. Ask a follow-up or rate the answer. Feedback contributes to tool-outcome scoring.

Questions asking what to buy are routed as research/advice, not treated as orders. Conditional statements such as “if SOL drops” must not become an execution command.

### Inspect a wallet

1. Sign in to the account.
2. Connect a supported wallet or paste a public address for read-only inspection.
3. Ask about balances, positions, activity, or a scenario.
4. Review partial-data warnings. The default snapshot prices at most 300 holdings within a bounded time.

Disconnecting removes the wallet binding from the chat. A public address alone does not authorize transfers.

### Review a Solana swap

1. Name the input asset, output asset, amount, chain, and optional slippage.
2. Orbit resolves tokens, obtains a quote, checks platform caps, simulates the prepared transaction, and evaluates the risk charter.
3. Review the card, including amount, slippage, warnings, and expiry. The default plan lifetime is 120 seconds.
4. Confirm through the wallet flow. Typing `CONFIRM` in chat does not sign a transaction.
5. The browser wallet signs; the server validates the signed message against the reviewed plan before submission.
6. Track submitted/finalized/failed/unknown status. Submission is not settlement. An unknown result must be investigated rather than blindly repeated.

Default platform limits are $25 per trade, 100 basis points of slippage, and 5% price impact. These are deployment settings, not fixed product promises. User charter fields can impose stricter limits, allowed chains, position limits, verified-token restrictions, and notes. Structured limits are deterministic; free-text notes involve model interpretation.

`LIVE_TRADING=false` blocks the Jupiter wallet-transaction preparation/submission endpoints. A separate optional server-signing route exists; see [architecture](architecture.md). Do not assume every execution provider has identical enforcement boundaries.

### Review a cross-chain swap

Orbit prepares a Relay draft. The browser resolves the route and presents the quote, receiving wallet, and destination-gas considerations. The reviewed request is tied to the conversation and revision. A persisted Relay claim precedes wallet execution, and the backend checks provider status independently.

Some charter restrictions cannot be established for a cross-chain route; for example, a Jupiter-verified-only requirement cannot simply be presumed true. The UI rejects unsupported checks rather than treating them as satisfied. Relay browser checks and Jupiter server checks are separate implementations.

### Schedule follow-up work

Create a task in Settings or through chat. Supported kinds are reminders, price alerts, and briefs. Schedules support one-off timestamps and recurring forms. The service stores a UTC offset; this is not a full timezone/DST scheduling engine.

The worker checks approximately every 30 seconds. Price alerts normally recheck every five minutes. Results appear in the inbox, with optional email when configured. Pause/resume and Run now are available. Default active-task limits are Free 3, Pro 25, Max 100. A delivered brief costs one credit by default; reminders and price-alert delivery do not have that brief charge. Creating a task through chat still uses the chat turn's metering.

## Accounts, plans, and charging

The following is the code's catalogue, not a statement about a live commercial offer:

| Plan | Monthly list price | Allowance | API keys / MCP | Seats |
|---|---:|---:|---|---:|
| Anonymous trial | — | 10 once | No | — |
| Free | $0 | 100/month | No | 1 |
| Pro | $19 | 2,000/month | Yes | 1 |
| Max | $49 | 7,500/month | Yes | 5 |

Anonymous research uses a device identifier, falling back to IP. This is trial metering, not a verified identity. Email sign-in uses a one-time link, with a 15-minute default expiry. Development mode can expose the link directly when no email provider is configured; production must use the intended email setup.

Default chat costs: plain reply 1, data-tool turn 2, trade preparation 3, deep dive 5, team analysis 5. Credits are reserved before work and settled against the actual turn classification. Failed execution of the chat service releases its reservation. A credit balance and a payment-provider bill are different quantities.

Stripe handles subscription and pack checkout on hosted pages. The code includes 500-, 2,000-, and 10,000-credit packs. Their local display prices are placeholders; configured Stripe Prices determine actual checkout amounts. Without billing credentials/configuration, purchase endpoints return controlled unavailable errors.

**Entitlement caveat:** the catalogue's `team_mode` field currently defaults to true for signed-in plans, including Free, although the feature copy highlights it under Pro. Do not publish “Pro-only In-depth analysis” as an enforced restriction without resolving this discrepancy.

## What the product does not currently provide

- Stock order execution, unattended trading, or authority for a model to sign through a user's wallet.
- Permanent, cross-device synchronized conversation projects.
- Guaranteed complete wallet valuations, complete protocol coverage, or universally correct model answers.
- Enterprise organization-wide document access control for the shared Knowledge Base.
- The non-financial vertical products discussed as future possibilities.

Source: [UI](../../app/static/index.html), [admin UI](../../app/static/admin.html), [models](../../app/models.py), [plans](../../app/billing_plans.py), [credits](../../app/credits.py), [tasks](../../app/tasks.py), [sessions](../../app/sessions.py).
