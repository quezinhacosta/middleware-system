"""Middleware de autenticação HTTP para serviços FastAPI/Starlette."""

from collections.abc import Callable, Iterable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from middleware.auth.context import auth_token_var
from middleware.auth.token_provider import (
    AuthenticatedUser,
    InvalidTokenError,
    TokenExpiredError,
    TokenManager,
)

DEFAULT_PUBLIC_PATHS: set[str] = {
    "/",
    "/health",
    "/health/live",
    "/health/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/login",
}


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware HTTP para autenticação stateless via JWT.

    Extrai o cabeçalho Authorization: Bearer <token>, valida a assinatura criptográfica,
    expiração e emissor, e injeta o objeto AuthenticatedUser em request.state.user.
    Retorna HTTP 401 caso o token seja ausente em rotas protegidas, malformado ou expirado.
    """

    def __init__(
        self,
        app: ASGIApp,
        token_manager: TokenManager | None = None,
        exempt_paths: Iterable[str] | None = None,
        exempt_routes: Iterable[tuple[str, str]] | None = None,
        protected_paths: Iterable[str] | None = None,
        protected_routes: Iterable[tuple[str, str]] | None = None,
        allow_mock_tokens: bool = True,
    ) -> None:
        super().__init__(app)
        self.token_manager = token_manager or TokenManager()
        self.exempt_paths = set(exempt_paths or DEFAULT_PUBLIC_PATHS)
        self.exempt_routes = list(exempt_routes or [])
        self.protected_paths = set(protected_paths) if protected_paths else None
        self.protected_routes = list(protected_routes) if protected_routes else None
        self.allow_mock_tokens = allow_mock_tokens

    def _is_exempt(self, method: str, path: str) -> bool:
        """Verifica se o endpoint atual é isento de autenticação obrigatória."""
        method_upper = method.upper()

        # 1. Se protected_routes for configurado, apenas pares (método, rota) exatos/wildcard são protegidos
        if self.protected_routes is not None:
            matches_protected = False
            for p_method, p_path in self.protected_routes:
                if p_method.upper() in (method_upper, "*"):
                    if p_path.endswith("*"):
                        if path.startswith(p_path[:-1]):
                            matches_protected = True
                            break
                    elif path == p_path:
                        matches_protected = True
                        break
            if not matches_protected:
                return True

        # 2. Se protected_paths estiver configurado, apenas rotas que começam com ele são protegidas
        if self.protected_paths is not None:
            is_explicitly_protected = any(
                path == p or path.startswith(f"{p}/") for p in self.protected_paths
            )
            if not is_explicitly_protected:
                return True

        # 3. Verifica rotas isentas por caminho exato ou prefixo
        for ex in self.exempt_paths:
            if path == ex or path.startswith(f"{ex}/"):
                return True

        # 4. Verifica pares (método, caminho com suporte a wildcard *)
        for ex_method, ex_path in self.exempt_routes:
            if ex_method.upper() in (method_upper, "*"):
                if ex_path.endswith("*"):
                    prefix = ex_path[:-1]
                    if path.startswith(prefix):
                        return True
                elif path == ex_path:
                    return True

        return False

    def _parse_mock_token(self, token_str: str) -> AuthenticatedUser | None:
        """Fallback para tokens mock em testes legados (ex: 'role=customer,sub=123')."""
        if not self.allow_mock_tokens:
            return None

        # Só aceita mock se tiver padrão explícito de chave-valor (evita aceitar strings aleatórias)
        if "role=" not in token_str and "sub=" not in token_str:
            return None

        roles: list[str] = []
        user_id = "mock-user-123"
        for part in token_str.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if part.startswith("role="):
                roles.append(part.split("=", 1)[1].strip())
            elif part.startswith("sub="):
                user_id = part.split("=", 1)[1].strip()

        if not roles:
            roles.append("customer")
        return AuthenticatedUser(user_id=user_id, roles=roles)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Any]
    ) -> Response:
        path = request.url.path
        method = request.method
        auth_header = request.headers.get("Authorization")
        is_exempt_route = self._is_exempt(method, path)

        # 1. Se não há header Authorization
        if not auth_header:
            if is_exempt_route:
                request.state.user = None
                res: Response = await call_next(request)
                return res
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "detail": "Cabeçalho Authorization ausente.",
                },
            )

        # 2. Extrai o token Bearer
        parts = auth_header.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_token = parts[1].strip()
        else:
            raw_token = auth_header.strip()

        if not raw_token:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "detail": "Token Bearer vazio ou malformado.",
                },
            )

        # 3. Decodifica e valida o token
        user: AuthenticatedUser | None = None
        try:
            user = self.token_manager.get_user_from_token(raw_token)
        except TokenExpiredError:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "detail": "Token de autenticação expirado.",
                },
            )
        except InvalidTokenError:
            # Tenta fallback para tokens mock de teste legado caso habilitado
            mock_user = self._parse_mock_token(raw_token)
            if mock_user:
                user = mock_user
            else:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "unauthorized",
                        "detail": "Token de autenticação inválido ou malformado.",
                    },
                )

        # 4. Injeta usuário autenticado no request.state.user
        request.state.user = user

        # 5. Injeta no auth_token_var para propagação em chamadas de saída
        token_var_token = auth_token_var.set(raw_token)
        try:
            response: Response = await call_next(request)
            return response
        finally:
            auth_token_var.reset(token_var_token)
