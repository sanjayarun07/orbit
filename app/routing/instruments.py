"""Versioned instrument aliases used only for domain disambiguation.

This local seed can be extended without changing resolver code. It is not an
authoritative market listing; unknown symbols still require chain/identity.
"""

import json
from pathlib import Path
import re

REGISTRY = json.loads(Path(__file__).with_name("instruments.json").read_text())


def equity_instruments(request: str) -> list[str]:
    words = set(re.findall(r"[A-Za-z][A-Za-z0-9:.-]*", request.upper()))
    return [row["symbol"] for row in REGISTRY if words.intersection(row["aliases"])]
