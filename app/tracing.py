"""Optional Langfuse tracing. No-op unless both API keys are configured, so the
app runs identically with or without a Langfuse account set up.
"""

from langfuse import observe

from app.settings import settings

enabled = bool(settings.langfuse_public_key and settings.langfuse_secret_key)

if enabled:
    from langfuse import Langfuse

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )


def trace(*args, **kwargs):
    """Same signature as langfuse.observe; a passthrough no-op when tracing is disabled."""
    if enabled:
        return observe(*args, **kwargs)

    def noop(func):
        return func

    return noop
