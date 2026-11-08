"""Correlation ID: rastreia uma requisição entre serviços e logs.

O valor vem do header ``X-Correlation-ID`` quando presente e válido; caso
contrário, é gerado um UUID v4. Fica disponível via ``contextvars`` para os
logs e para as chamadas de saída.
"""

import re
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "X-Correlation-ID"

# Aceita apenas identificadores curtos e seguros, evitando injeção nos logs
_VALID_ID = re.compile(r"^[A-Za-z0-9-]{1,128}$")

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Retorna o correlation ID da requisição atual, se houver."""
    return correlation_id_var.get()


def correlation_headers() -> dict[str, str]:
    """Headers a anexar em chamadas de saída, para propagar o correlation ID."""
    correlation_id = correlation_id_var.get()
    return {CORRELATION_HEADER: correlation_id} if correlation_id else {}


class CorrelationIdMiddleware:
    """Middleware ASGI que define e devolve o correlation ID de cada requisição."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(CORRELATION_HEADER)
        correlation_id = (
            incoming if incoming and _VALID_ID.match(incoming) else str(uuid.uuid4())
        )

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        token = correlation_id_var.set(correlation_id)
        try:
            await self.app(scope, receive, send_with_header)
        finally:
            correlation_id_var.reset(token)
