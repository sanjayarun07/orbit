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
