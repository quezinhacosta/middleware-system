"""Contexto de autenticação para propagação em chamadas de saída."""

from contextvars import ContextVar

auth_token_var: ContextVar[str | None] = ContextVar("auth_token", default=None)


def get_auth_token() -> str | None:
    """Retorna o token de autorização da requisição/sessão atual, se houver."""
    return auth_token_var.get()


def set_auth_token(token: str | None) -> None:
    """Define o token de autorização no contexto da requisição atual."""
    auth_token_var.set(token)
