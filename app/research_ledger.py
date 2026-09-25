"""Source-grounded coverage state for a research turn.

Search snippets are discovery leads. Only exact passages from inspected pages
can support a requirement, and a candidate is complete only when each
qualifying condition has its own accepted passage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Status = Literal["supported", "contradicted", "unknown"]


@dataclass(frozen=True)
class EvidenceRecord:
    candidate: str
    requirement: str
    status: Status
    source_url: str = ""
    passage: str = ""
    fetched_at: str = ""
    event_time: str = ""
    provenance: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CoverageLedger:
    conditions: list[str]
    required_facts: list[str]
    records: list[EvidenceRecord] = field(default_factory=list)

    @classmethod
    def from_verdicts(cls, conditions: str, required_facts: str, verdicts: list[dict] | None) -> "CoverageLedger":
        terms = [part.strip() for part in conditions.split(";") if part.strip() and part.strip().lower() != "none"]
        facts = [part.strip(" -*\t") for part in required_facts.splitlines() if part.strip(" -*\t")]
        ledger = cls(terms, facts)
        for verdict in verdicts or []:
            candidate = verdict.get("name") or "unnamed candidate"
            mapped = {row["condition"]: row for row in verdict.get("condition_evidence") or []}
            for index, term in enumerate(terms, 1):
                row = mapped.get(index)
                if row and verdict.get("provenance") == "page":
                    ledger.records.append(EvidenceRecord(candidate, term, "supported", row["url"], row["quote"],
                                                         row.get("fetched_at", ""), row.get("event_time", ""), "page"))
                else:
                    # A different design is a candidate-level verdict. It
                    # does not identify which individual requirement was
                    # contradicted, so leave that requirement unknown.
                    ledger.records.append(EvidenceRecord(candidate, term, "unknown", note=verdict.get("note") or "not established by a checked passage"))
        return ledger

    def missing(self, candidate: str) -> list[str]:
        return [record.requirement for record in self.records if record.candidate == candidate and record.status != "supported"]

    def complete(self, candidate: str) -> bool:
        rows = [record for record in self.records if record.candidate == candidate]
        return bool(self.conditions and len(rows) == len(self.conditions) and all(row.status == "supported" for row in rows))

    def brief(self) -> str:
        lines = ["Required facts to answer:\n" + ("\n".join(self.required_facts) or "none stated")]
        if not self.records:
            return "\n".join(lines + ["No condition has an inspected supporting page passage yet."])
        for record in self.records:
            line = f"{record.candidate} | {record.status} | {record.requirement}"
            if record.status == "supported":
                line += f" | [{record.source_url}] {record.passage}"
            elif record.note:
                line += f" | {record.note}"
            lines.append(line)
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"conditions": self.conditions, "required_facts": self.required_facts,
                "records": [record.as_dict() for record in self.records]}
