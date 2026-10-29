"""Logs estruturados em JSON, uma linha por evento, emitidos no stdout."""

import json
import logging
import logging.config
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Preenchido pelo interceptor de Correlation ID (issue de propagação)
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Campos extras aceitos em `logger.info(..., extra={...})`
EXTRA_FIELDS = (
    "http_method",
    "http_path",
    "duration_ms",
    "status_code",
)

# Loggers do uvicorn, que precisam usar o mesmo formato dos logs da aplicação
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class CorrelationIdFilter(logging.Filter):
    """Anexa o correlation_id do contexto atual, sem exigir injeção manual."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = correlation_id_var.get()
        return True


class JsonLogFormatter(logging.Formatter):
    """Formata cada registro como um objeto JSON de linha única."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, UTC)
        payload: dict[str, Any] = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "service": self.service,
            "correlation_id": getattr(record, "correlation_id", None),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            # A stack trace vai como string única, para não quebrar a linha JSON
            payload["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configura o logging raiz e os loggers do uvicorn no formato JSON."""
    handler = {
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
        "formatter": "json",
        "filters": ["correlation_id"],
    }
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"correlation_id": {"()": CorrelationIdFilter}},
            "formatters": {"json": {"()": JsonLogFormatter, "service": service}},
            "handlers": {"stdout": handler},
            "root": {"handlers": ["stdout"], "level": level},
            "loggers": {
                name: {"handlers": ["stdout"], "level": level, "propagate": False}
                for name in UVICORN_LOGGERS
            },
        }
    )
