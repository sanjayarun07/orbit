# Equities-data gateway

Anvaya uses the internal equities-data HTTP gateway for company, macro and
market records sourced from financialdatasets.ai and FMP. The gateway is a
separate service. Configure these server-only variables in the ignored `.env`:

```text
EQUITIES_DATA_BASE_URL=https://your-equities-data-host
EQUITIES_DATA_INTERNAL_TOKEN=<internal-token>
```

The client is `app/equities_data.py`. Its endpoint catalog covers every unique
read endpoint in `equities-data.postman_collection.json`: market and company
news; historical, feed and upcoming earnings; insider trades; daily historical
prices and snapshots; ratings and consensus; most-active stocks; interest
rates, CPI and yield curve; company and feed filings; institutional holdings
and popular investors; ticker details; and Aster/Hyperliquid movers. Single-
and multi-ticker requests, date filters and ranges are query parameters of
those endpoints. Intraday historical bars are deliberately not requested:
the collection states that its current upstream does not support them.

`EquitiesDataProvider` registers these reads as provider tools. The existing
Tequity WebSocket and tick ledger remain the preferred source for Aster and
Hyperliquid movers; the HTTP movers endpoint is a lower-priority fallback.
Specific stock requests can reach matching gateway tools through the normal
provider router, and the equity-research path includes matching gateway
records alongside web context and a connected user's TradingView data.

The client also implements the collection's `/health`, user watchlist
add/remove/list, and single-key cache deletion endpoints. Watchlist calls
require a *user Bearer JWT* in addition to the server's internal token.
They are not exposed as chat tools and cannot be bound to Anvaya accounts
until the gateway's JWT issuer/identity mapping is confirmed. Cache deletion
is an operator method only; it is not callable from a research prompt.

Gateway response dates and links are retained in the evidence card. The
retrieval timestamp is not substituted for an event's publication time.
The collection's `sentiment` field is price-direction-derived, not textual
sentiment. Its unusual-volume endpoint is a most-active list, and 13F
holdings are delayed position snapshots rather than computed fund flows.
Tokenized-stock perp prices are not underlying share prices. Cached feeds
can return `not_ready` while being populated.

Tests in `tests/test_equities_data.py` cover endpoint mapping, routing,
authentication boundaries and bounded, valid JSON evidence. Live gateway
checks are separate because they require service credentials and can change
with upstream availability.
