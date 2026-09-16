# Orbit interface

The interface uses neutral surfaces, one indigo accent, and Inter throughout.
Home, chat, account settings, wallet dialogs, and administration share the same
light and dark tokens. The system preference remains the default.

## Files

- `app/static/theme.css`: shared colors, typography, spacing constants, motion,
  focus indicators, and reduced-motion behavior.
- `app/static/chat.css`: existing chat, data-card, and wallet component styles.
- `app/static/product.css`: product layout, home, settings, and responsive rules.
- `app/static/admin-theme.css`: administration layouts using the shared tokens.

Load the shared tokens first, component styles second, and product layout last.
Keep new colors in the shared tokens rather than adding page-specific palettes.

## Writing

Use short, sentence-case labels that describe the user's action. Explain the
result before implementation details. For example, use “Get quote”, “Trade
preferences”, and “In-depth” in the interface. Existing API fields and command
strings retain their names for compatibility.

Use specific next steps for empty states and failures. Configuration variable
names belong in developer diagnostics, not wallet or billing prompts. Retain
explicit approval language for transactions and clear consequences for deletion.

## Interaction

- Keep the home composer first, with four stable starting actions below it.
- Show the market card grid only when highlights are available.
- Keep the full example catalogue behind one disclosure.
- Use a vertical settings navigation on desktop and horizontal tabs on mobile.
- Show Save changes only for panels with editable preferences.
- Keep focus in dialogs, restore it on close, and close the top dialog on Escape.
- Support arrow keys and Home/End in settings navigation.
- Use 140 ms control transitions and 180 ms dialog entrances. Respect reduced
  motion. Avoid decorative loops, bouncing cards, and animated backgrounds.

## References reviewed, September 2026

- [Linear's March 2026 interface refresh](https://linear.app/now/behind-the-latest-design-refresh)
- [Vercel Geist typography](https://vercel.com/geist/typography)
- [Framer Stasis template](https://www.framer.com/marketplace/templates/stasis/)
- [Framer Dashflow template](https://www.framer.com/marketplace/templates/dashflow/)

These informed hierarchy, restrained styling, and navigation. No template code
or assets were copied.

## Recents (sidebar history)

Compact single-line rows under a "Recents" switcher (Recents / Pinned /
Archived). A row shows its project tag; pin and "more" appear on hover, focus,
or touch. The "more" menu offers Share (copies a link that opens for the
owner's account only), Rename (inline; also F2 on a focused row), Pin, Archive,
Delete (inline Delete/Keep confirmation, never a browser dialog; also the Delete
key) and Move to project (existing projects, a new one, or remove). Titles,
pins, archive state and projects live in this browser's storage with the chat
registry; the server keeps transcripts and ownership only.

## Explore more (home)

The examples, chips and capability catalogue sit folded under "Explore more"
beneath the market tiles. A click, a wheel or trackpad scroll down, or a swipe
up on touch reveals them with a height-and-fade transition (grid-track
animation, no measured heights); scrolling back up once the page is at the top
folds them away. The folded content is inert so keyboard focus never lands in
it. Reduced-motion users get an instant toggle and no chevron nudge.

## Wallet memory (chat)

A wallet connected during a conversation is remembered by the server for that
conversation: a later turn that names no wallet still uses it. Disconnecting
in the UI calls `DELETE /chat/wallet/{session_id}` so the server forgets it.
Any wallet-gated request made without a wallet (swap, portfolio, wallet
health) is parked; replying `connected` after connecting re-runs it.

## Identity display (sidebar chip, Settings > Account)

One function, `syncIdentityDisplay()`, is the only place that writes the
sidebar profile chip's name/avatar and the Settings "Linked wallets" row. A
connected wallet (any provider) always wins there over a signed-in email,
since it's the clearer signal of who is at the keyboard right now; signed
out with no wallet falls back to "Your account". Called on every
`renderAccount()` pass and from `setWalletConnected`/`setWalletDisconnected`
directly, so the chip updates immediately on connect/disconnect without
waiting on a server round trip, and never keeps showing a stale email after
sign-out (the previous bug: the old code only set `#profileName` inside
`renderAccount()`'s signed-in branch, with nothing to reset it on sign-out).

The "connected this session" wallet shown here is client-side only, never
persisted server-side -- see the code review note in
`docs/routing-architecture.md`-adjacent memory about `app/wallet_auth.py`
being unreachable from any UI button today (a real signature-verify-and-
link flow exists on the backend but nothing calls it).

## Wallet sign-in (no email required)

Three ways in, all equal: chat anonymously on trial credits; sign in by
email magic link; or sign in with a wallet alone. Connecting a wallet from
the picker (Phantom, MetaMask, Coinbase Wallet, WalletConnect, Privy email
wallet, or a pasted public address) never requires signing in first.
After a provider connects, the page asks it to sign a one-time challenge
(`POST /auth/wallet/challenge` then `/auth/wallet/verify`; Coinbase's SDK
bundle drives the equivalent `/auth/coinbase/*` pair itself). A valid
signature signs the user in exactly like the email link does: same cookie,
same account model, an email-less `users` row on first use, Free plan and
monthly credits. A wallet already linked to an account always resumes that
account; while signed in by email, a newly verified wallet links to that
account instead. Signing is best-effort and never blocks connecting: if the
user declines the signature prompt the wallet stays connected read-only,
not signed in. Solana signatures are Ed25519 over the challenge text; EVM
signatures are EIP-191 with ERC-1271/6492 for smart wallets.

## History: whose it is, and how long it lives

Signed out, the sidebar shows only the current browser session's chats --
they live in `sessionStorage` and are gone when the tab closes; nothing from
a past anonymous visit reappears. Signed in, the sidebar is the ACCOUNT's:
`GET /me/conversations` returns that user's conversations (newest first,
titled by their first user message, empty ones skipped) and the client
merges its own per-chat metadata (pin, archive, project, rename) by id.
Entries are stamped with an `owner`, so another account's chats never show,
and only owned entries persist to `localStorage`.

Retention matches that split: a conversation expires
`chat_history_ttl_seconds` (2h) after its last turn, but every turn a
signed-in account owns pushes the expiry to
`chat_history_signed_in_ttl_seconds` (30d) via `sessions.extend_retention`.
Before this, an account with hundreds of listed sessions could open none of
them -- the mapping in Postgres outlived the messages in Redis.

The client also stops asserting a `context_revision` it doesn't know
(`null` until history is hydrated) and retries once without one if the
server reports the conversation moved on, so "This chat changed in another
tab" can no longer answer a fresh message after a reload.
