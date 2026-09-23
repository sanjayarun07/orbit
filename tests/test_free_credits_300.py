"""Free is 300 credits a month for everyone (user decision, 2026-09-23); an
account already granted the old 100 this month receives the difference once."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from app import credits
from app.billing_plans import FREE

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def test_free_is_300_a_month():
    assert FREE.monthly_credits == 300 and "300 credits / month" in FREE.features


def test_an_account_granted_the_old_amount_this_month_is_topped_up_once():
    credits.reset()
    account = credits.user_account_id("u-free")
    old = replace(FREE, monthly_credits=100)
    asyncio.run(credits.ensure_monthly_grant(account, old, NOW))
    assert asyncio.run(credits.balance(account)) == 100
    asyncio.run(credits.ensure_monthly_grant(account, FREE, NOW))
    asyncio.run(credits.ensure_monthly_grant(account, FREE, NOW))            # idempotent
    assert asyncio.run(credits.balance(account)) == 300
    asyncio.run(credits.ensure_monthly_grant(account, old, NOW))             # a lowered allowance never claws back
    assert asyncio.run(credits.balance(account)) == 300
    asyncio.run(credits.ensure_monthly_grant(account, FREE, datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert asyncio.run(credits.balance(account)) == 600                      # next month: the full new allowance, no uplift row
    refs = [r["ref_id"] for r in asyncio.run(credits.history(account))]
    assert refs.count("month:free:2026-09:uplift:300") == 1 and not any("2026-10:uplift" in r for r in refs)
    credits.reset()
