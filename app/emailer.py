"""Transactional email through Resend (magic links, verification, receipts).

Without RESEND_API_KEY nothing is sent: the caller gets False and, in
development, exposes the link another way (see accounts.start_email_signin).
"""

from __future__ import annotations

import logging

import httpx

from app.settings import settings

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


def enabled() -> bool:
    return bool(settings.resend_api_key)


async def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one email. Returns False (never raises) when email is not configured
    or the provider rejects the message; the caller decides what that means."""
    if not enabled():
        logger.info("email not configured; would send %r to %s", subject, to)
        return False
    payload = {"from": settings.email_from, "to": [to], "subject": subject, "html": html}
    if text:
        payload["text"] = text
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                RESEND_URL, json=payload, headers={"Authorization": f"Bearer {settings.resend_api_key}"}
            )
        if response.status_code >= 400:
            logger.warning("email send failed: %s %s", response.status_code, response.text[:200])
            return False
        # The provider's id is what its dashboard shows delivery events under;
        # without it an "accepted but never arrived" report cannot be traced.
        try:
            message_id = response.json().get("id")
        except Exception:
            message_id = None
        logger.info("email accepted by provider: id=%s subject=%r to=%s", message_id, subject, to)
        return True
    except httpx.HTTPError:
        logger.warning("email send failed", exc_info=True)
        return False


def magic_link_html(link: str, product: str, code: str | None = None) -> str:
    code_block = (
        f"<p>Using the {product} app on your phone? Enter this code in the sign-in sheet instead:</p>"
        f'<p style="font-size:26px;letter-spacing:6px;font-weight:600">{code}</p>'
    ) if code else ""
    return (
        f"<p>Sign in to <strong>{product}</strong> with the button below. The link works once and "
        f"expires in {settings.magic_link_ttl_minutes} minutes.</p>"
        f'<p><a href="{link}" style="display:inline-block;padding:10px 18px;background:#5b5bd6;color:#fff;'
        f'border-radius:8px;text-decoration:none">Sign in</a></p>{code_block}'
        f"<p style=\"color:#666;font-size:12px\">If you did not request this, ignore this email.<br>{link}</p>"
    )
