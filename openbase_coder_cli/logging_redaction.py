"""Credential redaction shared by service and voice diagnostics."""
import logging
import re

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(authorization|x-api-key|api[_-]?key|token|access[_-]?token)"
    r"(['\"]?\s*[:=]\s*['\"]?)([^'\"\s,)}\]&#]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[-._~+/=A-Za-z0-9]+")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|access[_-]?token|machine[_-]?token|session[_-]?token)=)"
    r"([^&#\s]+)"
)


def redact_exception_text(value: object) -> str:
    text = _SECRET_VALUE_RE.sub(r"\1\2[redacted]", str(value))
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    return _QUERY_SECRET_RE.sub(r"\1[redacted]", text)


class CredentialRedactionFilter(logging.Filter):
    """Keep Uvicorn's positional access-formatter arguments intact."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_exception_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_exception_text(arg) if isinstance(arg, str) else arg for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact_exception_text(value) if isinstance(value, str) else value for key, value in record.args.items()}
        return True


def install_uvicorn_credential_redaction() -> None:
    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, CredentialRedactionFilter) for item in logger.filters):
            logger.addFilter(CredentialRedactionFilter())
