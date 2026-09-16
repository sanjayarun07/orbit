# UI and functionality QA — 16 September 2026

**Outcome: two reproducible UI issues remain. Core automated tests pass. Live external integration coverage is substantial but is not a complete production certification.**

Backend tested at `881133f`. The sidebar changes were committed as `761f89b` during testing; all six new sidebar workflows and sign-out were also verified against that latest commit. The main browser run used a frozen UI snapshot; its sole difference from the final commit is the final fallback anchor adjustment in `placeMenu`, covered by the latest sidebar run. No application code was modified by this QA work.

## Findings

### P2 — Token autocomplete is clipped above the viewport

At 1440×1000, open a new chat and type `$sol`. The popup opens above the home composer without a height limit or scrolling. Its first result is obscured by the top bar and cannot be clicked normally. Measured popup top: **−7 px**; first item top: **−6 px**. Playwright's normal click on the first suggestion times out, and the screenshot confirms the clipping.

Source: `app/static/chat.css:498`. Constrain the popup to available viewport space, enable vertical scrolling, and choose its placement according to available room.

Evidence: [screenshot](evidence/ticker-clipped.png), [latest measurements](evidence/ui-latest-sidebar-results.json).

### P2 — “Swap assets” does not guide users into a swap

Clicking the home shortcut sends `Start a cross-chain swap`. In repeated live tests this resolves to `general` and displays a generic request for a token, wallet, company, or topic, rather than asking for the input asset, destination chain, and amount. Explicit swap prompts successfully produce quotes, so the issue is the shortcut's entry flow.

Source: `app/static/index.html:1111`. Open an actionable swap form or provide a dedicated guided prompt for the missing swap fields.

Evidence: [screenshot](evidence/final-failure-30.png), [browser results](evidence/ui-final-results.json).

## Results

| Suite | Result |
|---|---|
| Python suite, `.venv/bin/pytest -q` | 640 passed, 1 skipped; one deprecation warning |
| Real PostgreSQL execution integration, explicitly enabled | 1 passed; covers claims, restart/reconciliation, Relay turn uniqueness |
| Browser executor unit tests | 6 passed |
| Relay build/check, `npm run check:relay` | Passed |
| Main browser suite | 35/37 checks pass after correcting the sign-out test's asynchronous wait; two actual issues above |
| New sidebar workflow checks | 6/6 pass on latest commit |
| Supplemental live API functionality | 14/14 pass after correcting the wallet-replay test to expect the endpoint's 401 response |
| Existing live prompt sweep | Initial run 29/33; two failed quote scenarios pass on targeted retry; remaining qualifications below |

Thus **41/43 distinct browser checks pass** after validated retests. The standalone dropdown measurement check is evidence, not an additional passing usability check. Raw files retain original failed assertions rather than silently rewriting test history.

### Browser coverage

- Home and composer; anonymous gates; email sign-in using local development verification; sign-out.
- All eight settings tabs; preference persistence; light/dark themes.
- API key creation/revocation; authenticated MCP initialization and tool discovery.
- Reminder creation, pause/resume, run now, inbox/read, deletion.
- Billing unavailable state.
- Sweep prompts for TVL explanation, market overview, wallet portfolio, Jupiter quote/cancel, risk limits.
- Feedback, persisted history, conversation deletion.
- Latest sidebar rename, pin/view filtering, projects, copied account-only link, archive/restore, delete confirmation.
- Admin connection, account search/detail, provider dashboard, routing/classifier preview.
- Separate Knowledge Base page, search, populated results and citations.
- Desktop 1440×1000 and mobile 390×844; mobile navigation/settings/admin checked for horizontal overflow.
- No browser JavaScript exceptions observed in the main run.

### Other functionality

Live tests covered account isolation, conversation ownership, stale revisions, account export, API-key scope enforcement, task ownership, temporary-wallet authentication and nonce replay denial, team invitations/acceptance/access/leave, session revocation, input validation, and account deletion.

The existing `scripts/e2e_sweep.py` supplied the sample prompts. Passing scenarios include market research, narratives, token deep dives, ambiguity handling, security/holders/news, advice, context, portfolio/simulation, cancellation, Relay wallet handoff, team modes, home/calendar/sentiment, scheduled briefings, credits, and history. The initial “typed CONFIRM” and over-cap quote cases failed to produce a quote; both passed when repeated. Their initial quote failures remain an intermittent observation; the exact external cause was not isolated.

### Knowledge Base and sweep qualifications

- QA started with an empty database. Bootstrapped 50 protocols, 587 entities, and 537 relationships; derived 62 competitor relationships.
- Ingested three actual stablecoin documents (USDe, USDC, USDT), producing six chunks and 141 relationships. Live search returned six hits and six citations; the browser rendered populated results.
- Aave docs ingestion fetched three pages and wrote 27 chunks. Protocol data ingestion was also exercised for Aave and EigenCloud. EigenCloud docs discovery returned zero documents, so its docs coverage remains unverified.
- An Aave incident ingestion failed because the temporary PostgreSQL database was SQL_ASCII. Retesting against a UTF-8 copy succeeded with no ingestion errors. This was a test-environment issue, not a confirmed application defect.
- The original comprehensive Knowledge Base sweep requires a corpus exceeding 1,000 chunks plus specific incident/funding/graph facts. That corpus was not built in this isolated test; this scenario is **not certified as passing**.
- After seeding, the previously failing USDe question passed. The fees/yields scenario's rerun failed only its exact-tool assertions because those two answers came from `semantic_cache`; their fresh-tool assertions had passed in the initial run. This is a cache-sensitive sweep assertion, not evidence of a new product failure. The entire scenario did not achieve a clean single-run pass.

## Environment and limits

Used disposable PostgreSQL and Redis instances and separate local application ports. Real configured LLM/data providers were used for the live sweep and selected UI prompts; automated unit tests retain their own mocks. Paid provider calls may consume configured API credits.

Email delivery and live trading were disabled on the isolated app. No real wallet transaction was signed or broadcast; the temporary wallet signed only an authentication message. Real extension/mobile-wallet handoffs, funded settlement, real email delivery, and configured Stripe checkout/webhooks were not tested end to end. Their automated checks do not substitute for live integration validation. Testing was Chromium-based; Safari/Firefox, exhaustive accessibility, load, and full production corpus coverage were not included.

Some providers returned quota/access errors during the live run; fallback paths allowed most research cases to pass. These results describe observed behavior with the configured providers, not an availability guarantee.

## Evidence

Sanitized per-check results and selected screenshots are in [evidence](evidence/). Credentials, cookies, private wallet keys, provider logs, and the disposable database dump are excluded. The report and evidence are the only workspace files added by this QA work.
