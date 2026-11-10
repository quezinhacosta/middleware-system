"""Contexto de autenticação para propagação em chamadas de saída."""

from contextvars import ContextVar

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

auth_token_var: ContextVar[str | None] = ContextVar("auth_token", default=None)


def get_auth_token() -> str | None:
    """Retorna o token de autorização da requisição/sessão atual, se houver."""
    return auth_token_var.get()


def set_auth_token(token: str | None) -> None:
    """Define o token de autorização no contexto da requisição atual."""
    auth_token_var.set(token)


class AuthContextMiddleware:
    """Middleware ASGI para gerenciar o ciclo de vida do token de autorização.

    Extrai o header 'Authorization' de requisições HTTP de entrada, define em
    auth_token_var e garante reset seguro no bloco finally, impedindo vazamento de tokens
    entre requisições concorrentes ou threads reutilizadas no pool.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        auth_header = headers.get("authorization")
        token = None
        if auth_header:
            token = (
                auth_header.split(" ", 1)[1].strip()
                if auth_header.lower().startswith("bearer ")
                else auth_header.strip()
            )

        cv_token = auth_token_var.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            auth_token_var.reset(cv_token)
