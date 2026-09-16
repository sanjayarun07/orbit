"""Transport-neutral failures shared by application services."""
import re


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str | dict, headers: dict[str, str] | None = None):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


def safe_detail(exc: Exception, fallback: str) -> str:
    """Preserve short validation guidance without reflecting provider payloads or HTML."""
    detail = str(exc).strip()
    if not detail or len(detail) > 400 or "<!DOCTYPE" in detail or "jsonrpc" in detail.lower():
        return fallback
    return re.sub(r"\s+", " ", detail)
