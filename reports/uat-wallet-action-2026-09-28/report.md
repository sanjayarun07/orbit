# Wallet and action-flow UAT probe (2026-09-28)

Environment: signed-in local UI at `http://127.0.0.1:8000/ui/`, research deployment, current checkout `6fc5179c`. No trade was signed or submitted. The public Solana address below was provided by the user for testing.

## Live browser results

| Flow | Result | Evidence |
| --- | --- | --- |
| Bind public address `3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS` | Pass | Wallet button changed to `3aHL…KZkS · view only` and explicitly said this wallet cannot sign. |
| Ask to prepare a sale of 1 ANSEM, at 50 bps, without signing | Fail: wrong intent | “With this view-only wallet, prepare a sale of 1 ANSEM for USDC at max 50 bps. Do not sign or submit anything.” returned a generic explanation of how public-wallet lookup works. It did not address the requested action or the deployment gate. |
| Direct sale request | Pass: mode gate | “Sell 1 ANSEM for USDC from this wallet. Prepare only; do not execute.” stated that research mode cannot prepare or sign swaps. No confirm card or transaction appeared. |
| Read-only quote request | Fail: unnecessary clarification | “Read-only quote: what would selling 1 ANSEM for USDC return now at 50 bps? Do not prepare or submit a transaction.” asked for the token, wallet, and whether the user wanted research or transaction preparation, although all were supplied or bound. |
| Alternate read-only quote request | Fail: false balance claim | “What would I get if I sold 1 ANSEM for USDC right now? 50 bps slippage. Quote only.” said the wallet held no ANSEM. The immediately following canonical exit analysis for the same address returned **11.522238 ANSEM** and a fresh Jupiter full-exit quote of **$1.83**, timestamped `2026-09-28 12:10 UTC`. |
| Wallet chooser | Partial | Email wallet, Coinbase, Phantom, and MetaMask options appeared. WalletConnect was disabled as “Currently unavailable.” A real provider connection and signature were not exercised. |
| Inbox one-tap “Prepare the sale” | Not live-testable with current account | The inbox had a morning brief but no armed exit alert carrying the action. The current deployment is in research mode, so a tapped sell prompt should resolve to the mode refusal. |

## Existing controlled checks

`tests/test_agent_rules.py`, `tests/test_research_mode_swap_answer.py`, `tests/test_wallet_execution.py`, and `tests/test_ui_swap_flow.py` passed together: **41 passed**. The inbox DOM harness verifies that an armed alert's button sends its exact sell line into chat and closes the panel. These tests do not establish that a live alert is generated, delivered, tapped, and routed correctly in the browser.

## Provider handoff / remaining acceptance

1. Investigate the contradictory zero-ANSEM claim versus the exit-analysis balance for the same bound address. A read-only quote must either use the fresh wallet position or explicitly state that balance data is unavailable; it must not assert zero on a failed or different lookup.
2. Route explicit preparation requests to the deployment/action gate even when phrased with “view-only wallet.” Research mode should give the same clear refusal as the direct sell prompt.
3. Preserve the token, amount, output asset, slippage, and bound wallet when a user asks for a quote. If a quote is unsupported, state the actual missing field or provider limitation.
4. Supply a controlled armed-exit inbox fixture and a test wallet/provider in an execution-enabled test deployment to verify: alert delivery → one-tap prompt → fresh quote → confirmation card → wallet approval request → cancellation and stale-plan rejection. Do not broadcast a transaction in UAT.
