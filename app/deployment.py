"""Deployment modes and startup configuration validation (STAB-02).

Two independent axes, deliberately separate because they answer different
questions:

* ``deployment_mode`` -- may this deployment move money at all?  ``research``
  refuses every money-moving entry point across *all* providers; ``execution``
  allows them subject to the existing per-trade policy.  This is the gate that
  makes a research beta actually a research beta.
* ``allow_custodial_signing`` -- may the *server* hold a key and sign on the
  user's behalf?  Off unless explicitly turned on, because a wallet-only
  product should never be signing for anyone.

``live_trading`` predates both and is kept as the legacy switch: when
``deployment_mode`` is unset it is what the mode is inferred from, so existing
deployments and tests keep their behaviour.  Setting both, contradictorily, is
a fatal configuration error rather than a silent winner.

The audit below is the other half: a production deployment must fail to start
on a configuration that is unsafe rather than come up and quietly behave like a
laptop.  Every problem is collected before raising, so an operator fixes the
whole list in one cycle instead of discovering them one restart at a time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.settings import Settings, settings

logger = logging.getLogger(__name__)

RESEARCH = "research"
EXECUTION = "execution"
MODES = (RESEARCH, EXECUTION)

FATAL = "fatal"
WARNING = "warning"


class ExecutionDisabledError(RuntimeError):
    """A money-moving entry point was reached in a research deployment.

    Deliberately not a ``ValueError``: the trade routes convert those into a
    generic HTTP 400 "could not be completed", which would hide the real reason.
    This one is mapped to its own 403 with the message intact.
    """


class UnsafeDeploymentError(RuntimeError):
    """Startup configuration is unsafe for the declared environment."""


@dataclass(frozen=True)
class ConfigProblem:
    code: str
    severity: str
    setting: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"[{self.severity}] {self.code} ({self.setting}): {self.message}"


def _config(config: Settings | None = None) -> Settings:
    return config if config is not None else settings


def deployment_mode(config: Settings | None = None) -> str:
    """The effective mode, inferring from ``live_trading`` when unset."""
    config = _config(config)
    declared = (getattr(config, "deployment_mode", None) or "").strip().lower()
    if declared in MODES:
        return declared
    if declared:
        # An unrecognised value must never silently open execution.
        return RESEARCH
    return EXECUTION if config.live_trading else RESEARCH


def execution_enabled(config: Settings | None = None) -> bool:
    """True when this deployment may move money by any provider."""
    return deployment_mode(config) == EXECUTION


def custodial_signing_enabled(config: Settings | None = None) -> bool:
    """True when the server may sign with its own key on a user's behalf."""
    config = _config(config)
    return (
        execution_enabled(config)
        and bool(getattr(config, "allow_custodial_signing", False))
        and bool(config.solana_private_key)
    )


def require_execution_enabled(action: str, config: Settings | None = None) -> None:
    """Gate a money-moving entry point. Uniform across Jupiter, Relay and LI.FI."""
    if execution_enabled(config):
        return
    raise ExecutionDisabledError(
        f"{action} is unavailable: this deployment runs in research mode "
        "(DEPLOYMENT_MODE=research), where no provider may move funds."
    )


def require_custodial_signing(config: Settings | None = None) -> None:
    """Gate the one route where the server itself holds the key."""
    config = _config(config)
    require_execution_enabled("Server-side signing", config)
    if not getattr(config, "allow_custodial_signing", False):
        raise ExecutionDisabledError(
            "Server-side signing is disabled. This deployment is wallet-only: "
            "set ALLOW_CUSTODIAL_SIGNING=true to enable a server-held key."
        )


# --- startup configuration audit --------------------------------------------

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _is_production(config: Settings) -> bool:
    return (getattr(config, "environment", "development") or "").strip().lower() == "production"


def audit(config: Settings | None = None) -> list[ConfigProblem]:
    """Every configuration problem, worst first. Never raises."""
    config = _config(config)
    production = _is_production(config)
    problems: list[ConfigProblem] = []

    def add(code: str, severity: str, setting: str, message: str) -> None:
        problems.append(ConfigProblem(code, severity, setting, message))

    declared = (getattr(config, "deployment_mode", None) or "").strip().lower()
    if declared and declared not in MODES:
        add(
            "mode-unrecognised", FATAL, "DEPLOYMENT_MODE",
            f"{declared!r} is not a deployment mode. Use one of: {', '.join(MODES)}.",
        )
    elif declared == RESEARCH and config.live_trading:
        add(
            "mode-contradicts-live-trading", FATAL, "DEPLOYMENT_MODE",
            "DEPLOYMENT_MODE=research and LIVE_TRADING=true contradict each other. "
            "Research mode refuses every money-moving route, so say which you mean.",
        )
    elif declared == EXECUTION and not config.live_trading:
        # The other half of the same contradiction, and the more dangerous one:
        # it used to pass the audit and report execution enabled in
        # /config/public while the legacy flag still refused every Jupiter
        # route. A deployment must not advertise what it will not do.
        add(
            "mode-contradicts-live-trading", FATAL, "DEPLOYMENT_MODE",
            "DEPLOYMENT_MODE=execution and LIVE_TRADING=false contradict each other. "
            "Execution mode advertises that this deployment can move funds, so set "
            "LIVE_TRADING=true, or use DEPLOYMENT_MODE=research.",
        )
    elif not declared and production:
        add(
            "mode-not-declared", WARNING, "DEPLOYMENT_MODE",
            f"No mode declared; inferred {deployment_mode(config)!r} from LIVE_TRADING. "
            "Set DEPLOYMENT_MODE explicitly in production.",
        )

    if config.live_trading and config.solana_private_key and not getattr(config, "allow_custodial_signing", False):
        add(
            "custodial-key-present", FATAL, "SOLANA_PRIVATE_KEY",
            "A server signing key is configured with live trading on, but custodial "
            "signing is not enabled. Remove the key, or set ALLOW_CUSTODIAL_SIGNING=true "
            "to state that this deployment signs for its users.",
        )
    if getattr(config, "allow_custodial_signing", False) and not config.solana_private_key:
        add(
            "custodial-key-missing", WARNING, "ALLOW_CUSTODIAL_SIGNING",
            "Custodial signing is enabled but SOLANA_PRIVATE_KEY is unset, so the "
            "server-signing route will fail at call time.",
        )

    if not config.openai_api_key:
        add(
            "model-key-missing", FATAL if production else WARNING, "OPENAI_API_KEY",
            "No model credential: every chat turn will fail.",
        )

    if production:
        if getattr(config, "dev_expose_magic_links", False):
            add(
                "magic-links-exposed", FATAL, "DEV_EXPOSE_MAGIC_LINKS",
                "Sign-in links are returned in the HTTP response whenever email "
                "delivery fails. Anyone who knows an address could sign in as them.",
            )
        if getattr(config, "allow_memory_fallback", False):
            add(
                "memory-fallback-allowed", FATAL, "ALLOW_MEMORY_FALLBACK",
                "Missing Postgres or Redis would fall back to per-process memory: "
                "retained history, trade plans and turn locks stop being shared "
                "between workers instead of failing visibly.",
            )
        if not config.database_url:
            add("database-missing", FATAL, "DATABASE_URL", "No durable store for accounts, plans or history.")
        if not config.redis_url:
            add("redis-missing", FATAL, "REDIS_URL", "No shared store for sessions, turn locks or retention.")
        if not config.mcp_api_key:
            add(
                "mcp-unauthenticated", FATAL, "MCP_API_KEY",
                "The /mcp endpoint accepts unauthenticated tool calls when no key is set.",
            )
        base = (config.public_base_url or "").lower()
        if any(host in base for host in _LOCAL_HOSTS) or not base.startswith("https://"):
            add(
                "public-base-url-local", FATAL, "PUBLIC_BASE_URL",
                f"{config.public_base_url!r} is not a public HTTPS origin. Sign-in and "
                "MCP hand-off links are built from it.",
            )
        if not config.admin_api_key:
            add(
                "admin-key-missing", WARNING, "ADMIN_API_KEY",
                "Admin routes will refuse every caller with HTTP 503 (fails closed).",
            )
        if not config.resend_api_key:
            add(
                "email-not-configured", WARNING, "RESEND_API_KEY",
                "No transactional email: magic-link sign-in and task email delivery "
                "cannot reach anyone. Wallet sign-in still works.",
            )
        if getattr(config, "x402_enabled", False) and "84532" in str(getattr(config, "x402_network", "")):
            add(
                "x402-testnet", WARNING, "X402_NETWORK",
                "x402 payments are enabled against a testnet; real payment will not settle.",
            )

    order = {FATAL: 0, WARNING: 1}
    return sorted(problems, key=lambda problem: (order.get(problem.severity, 2), problem.code))


def enforce(config: Settings | None = None) -> list[ConfigProblem]:
    """Raise on any fatal problem, log and return the warnings.

    Called from the application lifespan so an unsafe production configuration
    stops the process instead of serving traffic.
    """
    problems = audit(config)
    fatal = [problem for problem in problems if problem.severity == FATAL]
    warnings = [problem for problem in problems if problem.severity != FATAL]
    for warning in warnings:
        logger.warning("configuration: %s (%s) %s", warning.code, warning.setting, warning.message)
    if fatal:
        listed = "\n".join(f"  - {problem.setting}: {problem.message}" for problem in fatal)
        raise UnsafeDeploymentError(
            f"Refusing to start: {len(fatal)} unsafe setting(s) for "
            f"ENVIRONMENT={getattr(config or settings, 'environment', 'development')}.\n{listed}"
        )
    return warnings


def public_status(config: Settings | None = None) -> dict:
    """What the browser is told, so the UI can hide what the server refuses."""
    config = _config(config)
    return {
        "mode": deployment_mode(config),
        "execution_enabled": execution_enabled(config),
        "custodial_signing": custodial_signing_enabled(config),
    }
