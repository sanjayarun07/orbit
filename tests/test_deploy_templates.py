"""The deploy templates: the staging one defines the same keys as the beta
one (so a setting added to one is not forgotten in the other), each passes
the startup audit once its placeholders are filled, and the preflight
script refuses an unfilled one."""
import re
import subprocess
import sys
from pathlib import Path

from app import deployment
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ("deploy/beta.env.example", "deploy/staging.env.example")


def _keys(path):
    return sorted(set(re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / path).read_text(), re.M)))


def _filled(path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from preflight import load
    values = load(str(ROOT / path))
    for key in ("POSTGRES_PASSWORD", "RESEND_API_KEY", "ADMIN_API_KEY", "MCP_API_KEY", "OPENAI_API_KEY"):
        values[key] = values[key] or "filled"
    values["PUBLIC_BASE_URL"] = "https://staging.example.com"
    values["DATABASE_URL"], values["REDIS_URL"] = "postgresql://orbit:x@postgres:5432/orbit", "redis://redis:6379/0"
    return Settings(_env_file=None, **{k.lower(): v for k, v in values.items() if v != ""})


def test_the_two_templates_define_the_same_keys():
    assert _keys(TEMPLATES[0]) == _keys(TEMPLATES[1])


def test_each_template_boots_once_filled_and_is_audited_as_production():
    for path in TEMPLATES:
        config = _filled(path)
        assert deployment._is_production(config), path
        fatal = [p for p in deployment.audit(config) if p.severity == deployment.FATAL]
        assert fatal == [], (path, fatal)
    assert deployment.deployment_mode(_filled(TEMPLATES[1])) == "research"          # staging starts with swaps off
    assert deployment.deployment_mode(_filled(TEMPLATES[0])) == "execution"


def test_the_preflight_refuses_an_unfilled_template():
    run = subprocess.run([sys.executable, "scripts/preflight.py", TEMPLATES[1]], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "REFUSES TO BOOT" in run.stdout and "model-key-missing" in run.stdout
    assert "<staging.yourdomain.com>" not in run.stdout                    # values are never printed, placeholders included
