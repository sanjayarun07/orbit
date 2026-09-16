"""The release pipeline itself has to be predictable.

CI runs a bare `pytest -q`. If anything outside tests/ matches pytest's
file patterns, collection imports it -- and scripts/ holds operational
tools that talk to live services, one of which used to abort collection
with SystemExit at import time. These tests fail the build for that class
of mistake rather than letting it surface as a red pipeline later.
"""
import ast
import importlib.util
try:
    import tomllib            # 3.11+
except ModuleNotFoundError:   # 3.10 runs the suite locally
    import tomli as tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "node_modules", ".git", "reports", "data", "dist", "build"}


def _candidate_files():
    for path in ROOT.rglob("*.py"):
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            yield path


def test_no_pytest_named_files_live_outside_the_suite():
    stray = [str(p.relative_to(ROOT)) for p in _candidate_files() if p.relative_to(ROOT).parts[0] != "tests"]
    assert stray == [], (
        f"these match pytest's collection patterns but are not tests: {stray}. "
        "Rename them (scripts/smoke_*.py) so a bare `pytest -q` cannot import them."
    )


def test_bare_pytest_is_pinned_to_the_suite():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert config.get("testpaths") == ["tests"], "bare `pytest -q` must collect tests/ and nothing else"
    assert "scripts" in config.get("norecursedirs", [])


@pytest.mark.parametrize("script", sorted(p for p in (ROOT / "scripts").glob("*.py")))
def test_every_script_is_safe_to_import(script):
    """No script may run work, exit, or call a live service at import time --
    importing is exactly what pytest collection does."""
    source = script.read_text()
    tree = ast.parse(source, filename=str(script))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Expr)):
            continue
        if isinstance(node, ast.If):   # `if __name__ == "__main__":` and simple guards are fine
            continue
        pytest.fail(f"{script.name} executes {type(node).__name__} at import time; move it into main()")


def test_settings_never_renders_a_credential():
    """`repr(settings)` reaches CI logs through any assertion or exception that
    touches a setting. A failing comparison against `settings.anything` renders
    the whole object, so masking has to live on the object, not at call sites.
    """
    from app.settings import Settings, _mask_secret, settings

    rendered = repr(settings)
    for name in ("openai_api_key", "admin_api_key", "stripe_secret_key", "mcp_api_key",
                 "solana_private_key", "resend_api_key", "stripe_webhook_secret"):
        value = getattr(settings, name, None)
        if value:
            assert value not in rendered, f"{name}'s value appears in repr(settings)"

    # And the masking rule itself, so a newly added credential is covered by
    # its name rather than by someone remembering to add it to a list.
    assert "swordfish" not in _mask_secret("some_new_api_key", "swordfish")
    assert "swordfish" not in _mask_secret("provider_secret", "swordfish")
    assert _mask_secret("public_base_url", "https://orbit.example.com") == "https://orbit.example.com"
    # A connection string carries its password inline.
    assert "hunter2" not in _mask_secret("database_url", "postgresql://app:hunter2@db:5432/orbit")
    assert isinstance(Settings().model, str)
