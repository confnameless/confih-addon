"""Logging with secret redaction.

The addon handles two kinds of secret: provider credentials (tokens, app/client
secrets, passwords) and optional path/admin secrets in URLs. Neither may reach
a log. The redaction filter is deliberately blunt: it replaces any configured
or dynamically registered secret verbatim and scrubs common query-parameter
spellings. A missed log line is better than a leaked token.
"""

from __future__ import annotations

import logging
import re
import threading
import weakref
from collections.abc import Iterable

_QUERY_SECRET_RE = re.compile(
    r"(?i)\b(user_auth_token|request_sig|app_secret|access_token|refresh_token|client_secret|password|token|hmac|sig)=([^&\s\"']+)"
)

_LOCK = threading.RLock()
_ACTIVE_SECRETS: set[str] = set()
_FILTERS: weakref.WeakSet[RedactionFilter] = weakref.WeakSet()
_FORMATTERS: weakref.WeakSet[RedactingFormatter] = weakref.WeakSet()


def _normalize_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    cleaned: set[str] = set()
    for s in secrets:
        if not isinstance(s, str):
            continue
        if len(s) >= 6:
            cleaned.add(s)
        stripped = s.strip()
        if len(stripped) >= 6:
            cleaned.add(stripped)
    return tuple(sorted(cleaned, key=len, reverse=True))


def _redact_text(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, "***")
    return _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)


class RedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self._own_secrets = _normalize_secrets(secrets)
        with _LOCK:
            self._secrets = _normalize_secrets((*self._own_secrets, *_ACTIVE_SECRETS))
            _FILTERS.add(self)

    def set_runtime_secrets(self, active_secrets: Iterable[str]) -> None:
        with _LOCK:
            self._secrets = _normalize_secrets((*self._own_secrets, *active_secrets))

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = self._secrets
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - logging should never raise
            return True

        record.msg = _redact_text(message, secrets)
        record.args = ()
        if record.exc_text:
            record.exc_text = _redact_text(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = _redact_text(record.stack_info, secrets)
        return True


class RedactingFormatter(logging.Formatter):
    def __init__(
        self,
        secrets: Iterable[str] = (),
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        base_formatter: logging.Formatter | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._base_formatter = base_formatter
        self._own_secrets = _normalize_secrets(secrets)
        with _LOCK:
            self._secrets = _normalize_secrets((*self._own_secrets, *_ACTIVE_SECRETS))
            _FORMATTERS.add(self)

    def set_runtime_secrets(self, active_secrets: Iterable[str]) -> None:
        with _LOCK:
            self._secrets = _normalize_secrets((*self._own_secrets, *active_secrets))

    def formatException(self, ei) -> str:  # type: ignore[override]
        rendered = (
            self._base_formatter.formatException(ei)
            if self._base_formatter is not None
            else super().formatException(ei)
        )
        return _redact_text(rendered, self._secrets)

    def formatStack(self, stack_info: str) -> str:
        rendered = (
            self._base_formatter.formatStack(stack_info)
            if self._base_formatter is not None
            else super().formatStack(stack_info)
        )
        return _redact_text(rendered, self._secrets)

    def format(self, record: logging.LogRecord) -> str:
        secrets = self._secrets
        if record.exc_text:
            record.exc_text = _redact_text(record.exc_text, secrets)
        rendered = (
            self._base_formatter.format(record)
            if self._base_formatter is not None
            else super().format(record)
        )
        if record.exc_text:
            record.exc_text = _redact_text(record.exc_text, secrets)
        return _redact_text(rendered, secrets)


def _attach_logger_redaction(
    logger: logging.Logger,
    secrets: tuple[str, ...],
    *,
    reset_existing: bool = False,
) -> None:
    existing_filters = [f for f in logger.filters if isinstance(f, RedactionFilter)]
    if reset_existing:
        for flt in existing_filters:
            logger.removeFilter(flt)
        logger.addFilter(RedactionFilter(secrets))
    elif not existing_filters:
        logger.addFilter(RedactionFilter(secrets))

    for handler in list(logger.handlers):
        h_filters = [f for f in handler.filters if isinstance(f, RedactionFilter)]
        if reset_existing:
            for flt in h_filters:
                handler.removeFilter(flt)
            handler.addFilter(RedactionFilter(secrets))
        elif not h_filters:
            handler.addFilter(RedactionFilter(secrets))

        fmt = handler.formatter
        if isinstance(fmt, RedactingFormatter):
            if reset_existing:
                fmt._own_secrets = _normalize_secrets(secrets)
                fmt.set_runtime_secrets(secrets)
        else:
            handler.setFormatter(
                RedactingFormatter(
                    secrets=secrets,
                    base_formatter=fmt if fmt is not None else logging.Formatter(),
                )
            )


def register_secrets(secrets: Iterable[str]) -> None:
    """Thread-safely register runtime secrets on active redaction filters and formatters."""
    normalized = _normalize_secrets(secrets)
    if not normalized:
        return
    with _LOCK:
        _ACTIVE_SECRETS.update(normalized)
        active_snapshot = tuple(_ACTIVE_SECRETS)
        for logger_name in ("uvicorn", "uvicorn.error"):
            _attach_logger_redaction(
                logging.getLogger(logger_name),
                active_snapshot,
                reset_existing=False,
            )
        filters = list(_FILTERS)
        formatters = list(_FORMATTERS)
    for flt in filters:
        flt.set_runtime_secrets(active_snapshot)
    for fmt in formatters:
        fmt.set_runtime_secrets(active_snapshot)


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    secret_list = _normalize_secrets(secrets)
    with _LOCK:
        _ACTIVE_SECRETS.clear()
        _ACTIVE_SECRETS.update(secret_list)
        active_snapshot = tuple(_ACTIVE_SECRETS)

        handler = logging.StreamHandler()
        handler.setFormatter(
            RedactingFormatter(
                secrets=active_snapshot,
                fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        handler.addFilter(RedactionFilter(active_snapshot))

        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level.upper())

        for logger_name in ("uvicorn", "uvicorn.error"):
            _attach_logger_redaction(
                logging.getLogger(logger_name),
                active_snapshot,
                reset_existing=True,
            )

        filters = list(_FILTERS)
        formatters = list(_FORMATTERS)

    for flt in filters:
        flt.set_runtime_secrets(active_snapshot)
    for fmt in formatters:
        fmt.set_runtime_secrets(active_snapshot)

    # Uvicorn's access log prints raw paths, including a path secret. Use the
    # app's own redacted request log instead.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
