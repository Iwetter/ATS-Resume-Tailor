"""Redaction for logs and audit records.

Logs and audit rows must never contain API keys, cookies, the applicant's
contact details or free-text answers. Redaction is applied to keys that are
known to be sensitive and to values that look like secrets or contact data.
"""
from __future__ import annotations

import json
import logging
import re

SENSITIVE_KEYS = re.compile(r"api[_-]?key|token|secret|password|cookie|authorization|session|email|phone|address|answer|value|first_name|last_name|full_name|gpa|birth|ssn", re.I)
SAFE_KEYS = {"lock_token_present", "token_budget", "input_tokens", "output_tokens", "max_output_tokens"}
PATTERNS = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[redacted-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[redacted-key]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"), "Bearer [redacted]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[redacted-email]"),
    (re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"), "[redacted-phone]"),
)
_extra_values: set[str] = set()


def register_secret(value: str | None):
    """Values (e.g. the applicant's name) that must never appear in logs."""
    if value and len(value.strip()) >= 3:
        _extra_values.add(value.strip())


def redact_text(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    for value in sorted(_extra_values, key=len, reverse=True):
        text = re.sub(re.escape(value), "[redacted]", text, flags=re.I)
    return text


def redact(value, key=""):
    if key and key not in SAFE_KEYS and SENSITIVE_KEYS.search(key):
        return "[redacted]" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line, redacted, for container log collectors."""

    def format(self, record):
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname, "logger": record.name,
                   "msg": redact_text(record.getMessage())}
        extra = getattr(record, "fields", None)
        if extra:
            payload["fields"] = redact(extra)
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level="INFO"):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger, level, msg, **fields):
    logger.log(level, msg, extra={"fields": fields} if fields else None)
