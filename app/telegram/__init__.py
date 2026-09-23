"""Orbit as a Telegram bot.

A transport, not a second product. Everything topical goes through the same
`app.execution_policy.execute_chat_turn` the web UI and the MCP server call,
so routing, the provider router, budgets, validation, credits, the risk
charter and persistence are shared rather than reimplemented. What the package
owns is the four decisions a transport is allowed to make:

- `identity.py` — a Telegram user is a way in to an ordinary Orbit account
- `bot.py`      — which conversation this is, and when a group message is ours
- `render.py`   — an AgentResponse as Telegram HTML and inline keyboards
- `progress.py` — the turn's status stream as one message that keeps updating
- `client.py`   — the four Bot API methods this needs
- `webhook.py`  — the HTTP edge, and the web UI's account-link endpoint

The whole package is inert until `TELEGRAM_BOT_TOKEN` is set: no route is
registered, no webhook is claimed, and nothing else in the app changes.

Signing stays where it has always been. Nothing here can sign, submit or move
funds; a quote is rendered as a review card with a button that opens the
conversation in the Orbit UI, where the user's own wallet confirms it.
"""

from app.telegram.client import enabled
from app.telegram.webhook import claim_webhook, router

__all__ = ["enabled", "claim_webhook", "router"]
