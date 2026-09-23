"""Typed facts: what a tool observed, as data, before any prose is written.

A provider's card is prose the next model repeats. A fact is a claim with a
subject, a value and unit, an observation time, a source and its coverage,
normalised once so the same classification holds everywhere: a null address
is a burn address, a pool pairing USDC with a volatile token is an LP, an
event's date is the day it happened. The fact gate (app/fact_gate.py) judges
answers against these, not against the card.

Facts are read from the cards the tools already write, by parsing their
markdown tables generically (header row -> keys), so every tool gains facts
without a second output format. A tool that wants richer facts can add them
to its evidence envelope instead.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

_BURN = re.compile(r"^(?:0x0{30,}(?:dead|0+)|1nc1nerator1{20,}|1{32})$", re.I)
_NUMBER = re.compile(r"^[+\-]?\$?\s*([\d,]*\.?\d+)\s*([kKmMbB%]?)$")
_FRESHNESS = re.compile(r"\*\*(?:Data freshness|Snapshot|Checked|Quoted|Provider)\*\*[^\n]*?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\s*UTC", re.I)
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b|\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)\b|\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{4})\b", re.I)


class Fact(BaseModel):
    kind: str                                   # ranking_row, holder_row, yield_row, event, quote, figure
    subject: str                                # symbol, address, headline
    value: float | None = None
    unit: str | None = None                     # pct, usd, count
    attrs: dict[str, Any] = Field(default_factory=dict)
    observed_at: str | None = None              # the provider's own time when the card states one
    event_date: str | None = None               # for events: the day it happened
    source: str = ""                            # tool name
    provider: str | None = None
    coverage: str = "complete"                  # complete | partial | unknown

    def label(self) -> str:
        v = f" {self.value:g}{'%' if self.unit == 'pct' else ''}" if self.value is not None else ""
        return f"{self.kind}:{self.subject}{v}"


def is_burn_address(address: str | None) -> bool:
    a = str(address or "").strip()
    return bool(a) and (bool(_BURN.match(a)) or a.lower().startswith("0x000000000000000000000000000000000000"))


def classify_address(address: str | None, labels: list[str] | None = None) -> str:
    """burn | pool | exchange | program | wallet -- a burn address wins over any provider label."""
    if is_burn_address(address):
        return "burn"
    text = " ".join(str(x) for x in (labels or [])).lower()
    if "pool" in text or "liquidity" in text or "amm" in text:
        return "pool"
    if "exchange" in text or "cex" in text:
        return "exchange"
    if "program" in text or "contract" in text or "pda" in text:
        return "program"
    return "wallet"


def parse_number(cell: str) -> tuple[float | None, str | None]:
    text = (cell or "").strip().strip("*").replace("—", "").replace("−", "-").strip()
    if not text:
        return None, None
    m = _NUMBER.match(text)
    if not m:
        return None, None
    value = float(m.group(1).replace(",", ""))
    suffix = m.group(2).lower()
    if suffix == "k":
        value *= 1e3
    elif suffix == "m":
        value *= 1e6
    elif suffix == "b":
        value *= 1e9
    unit = "pct" if suffix == "%" else "usd" if "$" in text else "count"
    if text.startswith("-") and value > 0:
        value = -value
    return value, unit


def parse_tables(markdown: str) -> list[list[dict[str, str]]]:
    """Every markdown table as a list of row dicts keyed by header text."""
    tables, rows, headers = [], [], None
    for line in (markdown or "").splitlines():
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if headers is None:
                headers = [re.sub(r"[*`]", "", c).strip() for c in cells]
                continue
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
        elif headers is not None:
            if rows:
                tables.append(rows)
            rows, headers = [], None
    if headers is not None and rows:
        tables.append(rows)
    return tables


_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\s*UTC", re.I)


def observed_at(markdown: str) -> str | None:
    """When the card's provider line says the data was seen: the latest UTC
    stamp on that line, so a ledger card's "From tick … To tick …" is as
    fresh as its last tick, not its baseline (the Aster losers episode read
    a four-hour-old period start as a stale card, 2026-09-23)."""
    m = _FRESHNESS.search(markdown or "")
    if not m:
        return None
    line = (markdown or "")[m.start():].split("\n", 1)[0]
    stamps = [s.replace(" ", "T") + ("" if len(s) > 16 else ":00") + "+00:00" for s in _STAMP.findall(line)]
    return max(stamps) if stamps else None


def _first(row: dict, *names: str) -> str | None:
    for key, value in row.items():
        k = key.lower()
        if any(n in k for n in names):
            return value
    return None


def facts_from_card(tool: str, markdown: str, kind_hint: str | None = None) -> list[Fact]:
    """Facts read from a tool's card. kind_hint is the contract kind, which
    decides how a table row is typed."""
    when = observed_at(markdown)
    out: list[Fact] = []
    tables = parse_tables(markdown)
    for rows in tables:
        for row in rows:
            keys = " ".join(k.lower() for k in row)
            if kind_hint == "holders" or "wallet" in keys and "share" in keys:
                address = _first(row, "wallet", "holder", "address", "owner")
                share, _ = parse_number(_first(row, "share", "supply", "%") or "")
                labels = (_first(row, "labels", "label", "tags") or "").split(",")
                out.append(Fact(kind="holder_row", subject=(address or "").strip("`"), value=share, unit="pct", source=tool, observed_at=when,
                                attrs={"class": classify_address((address or "").strip("`… "), labels), "labels": [l.strip() for l in labels if l.strip()],
                                       "value_usd": parse_number(_first(row, "value") or "")[0]}))
            elif kind_hint == "yields" or "apy" in keys:
                apy, _ = parse_number(_first(row, "apy") or "")
                exposure = (_first(row, "exposure") or "").lower()
                symbol = _first(row, "pool", "symbol") or ""
                is_lp = ("lp" in exposure or "multi" in exposure) or ("-" in symbol or "/" in symbol) and "single" not in exposure
                out.append(Fact(kind="yield_row", subject=symbol.strip("`"), value=apy, unit="pct", source=tool, observed_at=when,
                                attrs={"project": _first(row, "project"), "chain": _first(row, "chain"), "exposure": "lp" if is_lp else "single",
                                       "tvl_usd": parse_number(_first(row, "tvl") or "")[0]}))
            elif kind_hint == "portfolio" or ("balance" in keys and ("value" in keys or "share" in keys)):
                # A wallet's holdings card: Token | Balance | Price | USD Value | Share.
                value, _ = parse_number(_first(row, "usd value", "value") or "")
                out.append(Fact(kind="holding_row", subject=(_first(row, "token", "asset", "symbol") or "").replace("⚠", "").strip("` "), value=value, unit="usd", source=tool,
                                observed_at=when, attrs={"balance": parse_number(_first(row, "balance", "amount") or "")[0], "price": parse_number(_first(row, "price") or "")[0],
                                                         "share_pct": parse_number(_first(row, "share", "allocation") or "")[0]}))
            elif kind_hint == "transaction_intent" or any(k in keys for k in ("quoted proceeds", "minimum out", "expected output", "output amount")):
                # An exit or swap quote card: Exit | Tokens | Marked value | Quoted proceeds | Minimum out | Price impact | Route.
                proceeds, _ = parse_number(_first(row, "quoted proceeds", "expected output", "output amount", "output") or "")
                out.append(Fact(kind="quote_row", subject=(_first(row, "exit", "size", "sell", "pair") or "").strip("` "), value=proceeds, unit="usd", source=tool,
                                observed_at=when, attrs={"minimum_out": parse_number(_first(row, "minimum out", "minimum") or "")[0],
                                                         "impact_pct": parse_number(_first(row, "impact") or "")[0], "route": _first(row, "route"),
                                                         "tokens": parse_number(_first(row, "tokens", "amount") or "")[0]}))
            elif kind_hint == "market_ranking" or any(k in keys for k in ("change", "volume")):
                change, _ = parse_number(_first(row, "change") or "")
                volume, _ = parse_number(_first(row, "volume") or "")
                symbol = _first(row, "pair", "token", "symbol", "pool", "coin") or ""
                symbol = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", symbol).strip("`")
                out.append(Fact(kind="ranking_row", subject=symbol, value=change, unit="pct", source=tool, observed_at=when,
                                attrs={"volume_usd": volume, "liquidity_usd": parse_number(_first(row, "liquidity", "liq") or "")[0],
                                       "price": parse_number(_first(row, "price") or "")[0], "type": (_first(row, "type") or "").lower() or None,
                                       "venue": _first(row, "venue", "dex", "chain"), "rank": parse_number(_first(row, "#") or "")[0]}))
    if kind_hint == "transaction_intent" and not out:
        # A simulation written as prose: "would get you approximately **5.72737 USDC** ($5.73) ... **0.00% price impact**".
        m = re.search(r"approximately\s+\**([0-9][0-9,]*\.?[0-9]*)\s*([A-Za-z]{2,10})\**(?:\s*\(\$([0-9][0-9,]*\.?[0-9]*)\))?", markdown or "")
        if m:
            impact = re.search(r"([0-9]+(?:\.[0-9]+)?)%\s*price\s+impact", markdown or "", re.I)
            out.append(Fact(kind="quote_row", subject="simulation", value=parse_number(m.group(3) or m.group(1))[0], unit="usd" if m.group(3) else m.group(2).upper(),
                            source=tool, observed_at=when, attrs={"output_amount": parse_number(m.group(1))[0], "output_token": m.group(2).upper(),
                                                                  "impact_pct": float(impact.group(1)) if impact else None}))
    if kind_hint == "recent_events" and not out:
        for para in re.split(r"\n\s*\n", markdown or ""):
            text = para.strip()
            if len(text) < 30 or text.startswith("#") or text.startswith("|"):
                continue
            dates = [d for d in (m.group(1) or m.group(2) or m.group(3) for m in _DATE.finditer(text)) if d]
            out.append(Fact(kind="event", subject=text[:120], source=tool, observed_at=when, event_date=dates[0] if dates else None,
                            attrs={"dated": bool(dates), "text": text[:600]}))
    return out


def facts_from_search(found: dict, tool: str = "perplexity_web_search") -> list[Fact]:
    """Facts from a structured web search: one `source` fact per numbered
    source (url, date) and one `event` fact per dated paragraph, each carrying
    the markers [n] it cites so a claim traces to its source."""
    out: list[Fact] = []
    by_n = {s.get("n"): s for s in found.get("sources") or []}
    for s in found.get("sources") or []:
        out.append(Fact(kind="source", subject=s.get("title") or s.get("url") or "", source=tool, event_date=s.get("date"),
                        attrs={"url": s.get("url"), "n": s.get("n"), "date": s.get("date")}))
    for para in re.split(r"\n\s*\n", found.get("text") or ""):
        text = para.strip()
        if len(text) < 30 or text.startswith("#"):
            continue
        dates = [d for d in (m.group(1) or m.group(2) or m.group(3) for m in _DATE.finditer(text)) if d]
        cites = [int(n) for n in re.findall(r"\[(\d{1,2})\]", text)]
        urls = [by_n[n]["url"] for n in cites if n in by_n and by_n[n].get("url")]
        out.append(Fact(kind="event", subject=text[:120], source=tool, event_date=dates[0] if dates else None,
                        attrs={"dated": bool(dates), "text": text[:600], "cites": cites, "urls": urls, "traced": bool(urls)}))
    return out


def event_date_of(fact: Fact) -> datetime | None:
    raw = fact.event_date
    if not raw:
        return None
    try:
        from app.snapshot_compare import dates_in
        found = dates_in(raw)
        if found:
            return found[0]
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw.replace("Sept", "Sep").replace(".", ""), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    for fmt in ("%B %d", "%b %d"):
        try:
            d = datetime.strptime(raw.replace("Sept", "Sep").replace(".", ""), fmt)
            now = datetime.now(timezone.utc)
            d = d.replace(year=now.year, tzinfo=timezone.utc)
            return d if d <= now else d.replace(year=now.year - 1)
        except ValueError:
            continue
    return None
