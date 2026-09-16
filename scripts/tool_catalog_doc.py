"""Render app/tool_catalog.py to docs/tool-catalog.md.

    python scripts/tool_catalog_doc.py
"""
from pathlib import Path

from app.provider_registry import get_provider_router
from app.tool_catalog import catalog_markdown

out = Path(__file__).resolve().parents[1] / "docs" / "tool-catalog.md"
out.write_text(catalog_markdown(get_provider_router()))
print(f"wrote {out}")
