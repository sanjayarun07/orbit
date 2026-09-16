"""DefiLlama protocol detail as documents plus structured facts.

/protocol/<slug> carries far more than a description: the hallmark timeline
(launches, incentive programs, exploits), the oracles a protocol depends on,
what it was forked from, its parent family, every recorded hack with amount
and technique, and every funding round with investors. Each becomes text a
user can ask about ("has Aave been hacked", "who backs EigenLayer") and,
where it is a relationship, a graph edge with the document as provenance.

TVL, fees and prices stay live tools; nothing here is a number that moves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from app.knowledge.connectors.base import SourceRef
from app.knowledge.entities import slugify
from app.knowledge.models import NormalizedDocument, Protocol
from app.knowledge.normalize import clean_markdown, content_hash

PROTOCOL_URL = "https://api.llama.fi/protocol/{slug}"


def _date(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat() if value else None
    except (TypeError, ValueError, OSError):
        return None


def _money(amount, unit_millions: bool = False) -> str:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "undisclosed"
    if unit_millions:
        value *= 1_000_000
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    return f"${value:,.0f}"


def org_id(name: str) -> str:
    return f"org:{slugify(name)}"


class DefiLlamaConnector:
    name = "defillama"
    source_type = "defillama"
    refresh_every = timedelta(hours=24)

    def applies(self, protocol: Protocol) -> bool:
        return bool(protocol.defillama_slug)

    def discover(self, protocol: Protocol):
        return [SourceRef(url=PROTOCOL_URL.format(slug=protocol.defillama_slug), title=f"{protocol.name} — overview", kind="bundle")]

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        return None  # one fetch yields several documents; see documents()

    def documents(self, protocol: Protocol, ref: SourceRef) -> list[NormalizedDocument]:
        with httpx.Client(timeout=30, headers={"User-Agent": "Dopamint-Knowledge/0.1"}) as client:
            resp = client.get(ref.url)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
        return self.build(protocol, data, ref.url)

    @classmethod
    def build(cls, protocol: Protocol, data: dict, url: str) -> list[NormalizedDocument]:
        docs = [cls.document(protocol, data, url)]
        for hack in data.get("hacks") or []:
            doc = cls.incident_document(protocol, hack)
            if doc:
                docs.append(doc)
        funding = cls.funding_document(protocol, data.get("raises") or [])
        if funding:
            docs.append(funding)
        return docs

    # ---------------------------------------------------------------- overview
    @staticmethod
    def document(protocol: Protocol, data: dict, url: str) -> NormalizedDocument:
        chains = ", ".join(str(c) for c in (data.get("chains") or protocol.chains or []))
        audits = data.get("audit_links") or []
        oracles = [o for o in (data.get("oraclesBreakdown") or []) if isinstance(o, dict) and o.get("name")]
        forks = [f for f in (data.get("forkedFrom") or []) if isinstance(f, str)]
        parent = str(data.get("parentProtocol") or "").replace("parent#", "")
        family = [p for p in (data.get("otherProtocols") or []) if isinstance(p, str) and p != protocol.name]
        hacks = data.get("hacks") or []
        raises = data.get("raises") or []
        lines = [
            f"# {protocol.name}",
            "",
            f"**Category**: {data.get('category') or protocol.category or 'unknown'}",
            f"**Chains**: {chains or 'unknown'}",
            f"**Token**: {data.get('symbol') or protocol.symbol or 'none'}",
            f"**Website**: {data.get('url') or protocol.website or ''}",
            f"**Twitter**: {data.get('twitter') or protocol.twitter_handle or ''}",
        ]
        if parent:
            lines.append(f"**Family**: {parent.replace('-', ' ').title()}" + (f" (also: {', '.join(family[:8])})" if family else ""))
        if forks:
            lines.append(f"**Forked from**: {', '.join(forks)}")
        if oracles:
            lines.append("**Oracles**: " + ", ".join(f"{o['name']}" + (f" ({o['type']})" if o.get("type") else "") for o in oracles))
        lines += ["", "## Description", clean_markdown(str(data.get("description") or protocol.description or "No description provided."))]
        if data.get("methodology"):
            lines += ["", "## TVL methodology", clean_markdown(str(data["methodology"]))]
        if audits:
            lines += ["", "## Audits", f"{data.get('audits') or len(audits)} audit(s) on record.", *[f"- {a}" for a in audits[:10]]]
        if hacks:
            lines += ["", "## Security incidents"] + [
                f"- {_date(h.get('date')) or '?'}: {h.get('classification') or 'exploit'} via {h.get('technique') or 'unknown technique'}, {_money(h.get('amount'))} lost"
                + (f", {_money(h.get('returnedFunds'))} returned" if h.get("returnedFunds") else "") + (f" ({', '.join(h.get('chain') or [])})" if h.get("chain") else "")
                for h in hacks[:10]
            ]
        if raises:
            total = sum(float(r.get("amount") or 0) for r in raises)
            lines += ["", "## Funding", f"{len(raises)} round(s), {_money(total, unit_millions=True)} raised in total."] + [
                f"- {_date(r.get('date')) or '?'}: {r.get('round') or 'round'} of {_money(r.get('amount'), unit_millions=True)}"
                + (f" at {_money(r.get('valuation'), unit_millions=True)} valuation" if r.get("valuation") else "")
                + (f", led by {', '.join(r.get('leadInvestors') or [])}" if r.get("leadInvestors") else "")
                + (f", with {', '.join((r.get('otherInvestors') or [])[:6])}" if r.get("otherInvestors") else "")
                for r in sorted(raises, key=lambda r: -(int(r.get("date") or 0)))[:10]
            ]
        hallmarks = [h for h in (data.get("hallmarks") or []) if isinstance(h, (list, tuple)) and len(h) == 2]
        if hallmarks:
            lines += ["", "## Timeline"] + [f"- {_date(ts) or '?'}: {label}" for ts, label in sorted(hallmarks, key=lambda h: int(h[0] or 0))[:20]]
        if data.get("listedAt"):
            lines += ["", f"Listed on DefiLlama: {_date(data['listedAt'])}"]
        content = "\n".join(lines)
        facts = []
        now_meta = {"source": "defillama"}
        for o in oracles:
            facts.append({"relation": "USES_ORACLE", "target": {"id": org_id(o["name"]), "type": "organization", "name": o["name"]}, "confidence": 0.95,
                          "valid_from": o.get("startDate"), "metadata": {**now_meta, "oracle_type": o.get("type"), "proof": (o.get("proof") or [])[:3]}})
        for f in forks:
            facts.append({"relation": "FORK_OF", "target": {"type": "protocol", "name": f}, "confidence": 0.95, "metadata": now_meta})
        if parent:
            # The family entity carries no aliases on purpose: "Aave" must keep resolving
            # to the protocol, not to the umbrella; the PART_OF edges are the link.
            facts.append({"relation": "PART_OF", "target": {"id": org_id(parent), "type": "organization", "name": f"{parent.replace('-', ' ').title()} family", "aliases": []},
                          "confidence": 0.98, "metadata": {**now_meta, "family": family[:8]}})
        return NormalizedDocument(
            source="defillama", source_type="defillama", url=url, protocol_id=protocol.id, title=f"{protocol.name} — overview",
            content=content, content_hash=content_hash(content),
            metadata={"category": data.get("category"), "chains": data.get("chains"), "symbol": data.get("symbol"), "facts": facts},
        )

    # ---------------------------------------------------------------- incidents
    @staticmethod
    def incident_document(protocol: Protocol, hack: dict) -> NormalizedDocument | None:
        date = _date(hack.get("date"))
        if not date:
            return None
        chains = [c for c in (hack.get("chain") or []) if isinstance(c, str)]
        title = f"{hack.get('name') or protocol.name} exploit — {date}"
        lines = [
            f"# {title}", "",
            f"**Protocol**: {protocol.name} · **Date**: {date} · **Loss**: {_money(hack.get('amount'))}"
            + (f" · **Returned**: {_money(hack.get('returnedFunds'))}" if hack.get("returnedFunds") else "") + (f" · **Chains**: {', '.join(chains)}" if chains else ""),
            f"**Classification**: {hack.get('classification') or 'unknown'} · **Technique**: {hack.get('technique') or 'unknown'}"
            + (f" · **Target**: {hack.get('targetType')}" if hack.get("targetType") else "") + (f" · **Language**: {hack.get('language')}" if hack.get("language") and hack.get("language") != "None" else ""),
            "", "## What happened",
            f"{hack.get('name') or protocol.name} suffered a {str(hack.get('classification') or 'security').lower()} incident on {date}"
            f" using {str(hack.get('technique') or 'an unknown technique').lower()}, with an estimated {_money(hack.get('amount'))} lost"
            + (f" of which {_money(hack.get('returnedFunds'))} was later returned" if hack.get("returnedFunds") else "") + ".",
        ]
        if hack.get("source"):
            lines += ["", f"Source: {hack['source']}"]
        content = "\n".join(lines)
        incident_id = f"incident:{protocol.slug}:{date}"
        return NormalizedDocument(
            source="defillama_hacks", source_type="incident", url=f"https://defillama.com/hacks#{protocol.slug}-{date}", protocol_id=protocol.id, title=title,
            content=content, content_hash=content_hash(content), published_at=datetime.fromisoformat(date).replace(tzinfo=timezone.utc),
            metadata={"date": date, "amount_usd": hack.get("amount"), "returned_usd": hack.get("returnedFunds"), "technique": hack.get("technique"), "classification": hack.get("classification"), "chains": chains,
                      "facts": [{"relation": "HAD_INCIDENT", "confidence": 0.95, "valid_from": date,
                                 "target": {"id": incident_id, "type": "incident", "name": title, "metadata": {"date": date, "amount_usd": hack.get("amount"), "technique": hack.get("technique"), "classification": hack.get("classification")}},
                                 "metadata": {"source": "defillama", "amount_usd": hack.get("amount"), "technique": hack.get("technique")}}]},
        )

    # ---------------------------------------------------------------- funding
    @staticmethod
    def funding_document(protocol: Protocol, raises: list[dict]) -> NormalizedDocument | None:
        rounds = [r for r in raises if isinstance(r, dict) and (r.get("amount") or r.get("leadInvestors"))]
        if not rounds:
            return None
        rounds.sort(key=lambda r: int(r.get("date") or 0))
        total = sum(float(r.get("amount") or 0) for r in rounds)
        investors: dict[str, dict] = {}
        lines = [f"# {protocol.name} — funding rounds", "", f"**Rounds**: {len(rounds)} · **Total raised**: {_money(total, unit_millions=True)}", "", "## Rounds"]
        for r in rounds:
            date = _date(r.get("date")) or "?"
            leads = [i for i in (r.get("leadInvestors") or []) if isinstance(i, str)]
            others = [i for i in (r.get("otherInvestors") or []) if isinstance(i, str)]
            lines.append(f"- **{date}** · {r.get('round') or 'round'} · {_money(r.get('amount'), unit_millions=True)}"
                         + (f" at {_money(r.get('valuation'), unit_millions=True)} valuation" if r.get("valuation") else "")
                         + (f" · lead: {', '.join(leads)}" if leads else "") + (f" · also: {', '.join(others)}" if others else ""))
            for name in leads:
                entry = investors.setdefault(name, {"lead": 0, "rounds": []})
                entry["lead"] += 1
                entry["rounds"].append(date)
            for name in others:
                investors.setdefault(name, {"lead": 0, "rounds": []})["rounds"].append(date)
        if investors:
            lines += ["", "## Investors", ", ".join(sorted(investors, key=lambda n: (-investors[n]["lead"], n)))]
        content = "\n".join(lines)
        facts = [{"relation": "FUNDED_BY", "target": {"id": org_id(name), "type": "organization", "name": name}, "confidence": 0.95 if info["lead"] else 0.9,
                  "valid_from": min(d for d in info["rounds"] if d != "?") if any(d != "?" for d in info["rounds"]) else None,
                  "metadata": {"source": "defillama", "lead": bool(info["lead"]), "rounds": info["rounds"][:6]}} for name, info in investors.items()]
        return NormalizedDocument(
            source="defillama_raises", source_type="funding", url=f"https://defillama.com/raises#{protocol.slug}", protocol_id=protocol.id, title=f"{protocol.name} — funding rounds",
            content=content, content_hash=content_hash(content), metadata={"rounds": len(rounds), "total_usd": total * 1_000_000, "facts": facts},
        )
