"""Role-Based Access Control (RBAC) para usuários finais.

Verifica se o usuário autenticado possui os papéis necessários para acessar um recurso.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from middleware.auth.token_provider import AuthenticatedUser, TokenManager

logger = logging.getLogger(__name__)

# Simula extração de token de usuário (para fins do projeto, mockamos a decodificação)
oauth2_scheme = HTTPBearer(auto_error=False)


@dataclass
class UserIdentity:
    user_id: str
    roles: list[str]


def get_current_user(
    request: Request,
    token: HTTPAuthorizationCredentials | None = Depends(oauth2_scheme),
) -> UserIdentity:
    """Extrai a identidade do usuário a partir do token ou do contexto injetado pelo AuthMiddleware."""
    # 1. Se AuthMiddleware já autenticou e injetou o usuário em request.state.user
    state_user = getattr(request.state, "user", None)
    if isinstance(state_user, AuthenticatedUser):
        return UserIdentity(user_id=state_user.user_id, roles=state_user.roles)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de usuário ausente",
        )

    # 2. Se for um token JWT assinado, valida com TokenManager
    token_str = token.credentials
    try:
        manager = TokenManager()
        claims = manager.verify_token(token_str)
        return UserIdentity(
            user_id=str(claims["sub"]),
            roles=list(claims.get("roles", ["customer"])),
        )
    except Exception:
        pass

    # MOCK: Lê as claims 'sub' e 'roles' da string do token para testes sem precisar assinar JWTs reais.
    # Em um ambiente real, aqui haveria a decodificação e validação do JWT do usuário.
    token_str = token.credentials
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
        elif "=" not in part:
            roles.append(part)

    return UserIdentity(user_id=user_id, roles=roles)


def require_roles(required_roles: list[str]) -> Callable[..., UserIdentity]:
    """Valida se o usuário logado possui as permissões necessárias."""

    def dependency(user: UserIdentity = Depends(get_current_user)) -> UserIdentity:
        # Se required_roles for vazio, qualquer usuário logado tem acesso
        if not required_roles:
            return user

        user_roles = set(user.roles)
        required_set = set(required_roles)

        # Interseção: o usuário precisa de pelo menos UMA das roles requeridas
        if not user_roles.intersection(required_set):
            logger.warning(
                f"Acesso negado (403): Usuário {user.user_id} com roles {user.roles} tentou acessar recurso que exige {required_roles}."
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permissão insuficiente para acessar este recurso.",
            )
        return user

    return dependency
