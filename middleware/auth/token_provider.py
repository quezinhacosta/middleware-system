"""Gerenciamento de tokens JWT stateless para autenticação distribuída."""

import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import APIRouter
from pydantic import BaseModel, Field

from middleware.config.settings import get_settings

DEFAULT_ISSUER = "ecommerce-auth"
DEFAULT_ALGORITHM = "HS256"


@dataclass
class AuthenticatedUser:
    """Representação de usuário autenticado extraído do token JWT."""

    user_id: str
    roles: list[str]

    def has_role(self, role: str) -> bool:
        return role in self.roles


class AuthError(Exception):
    """Exceção base de autenticação."""

    pass


class TokenExpiredError(AuthError):
    """Lançada quando a assinatura do token expirou."""

    pass


class InvalidTokenError(AuthError):
    """Lançada quando o token é malformado, com assinatura inválida ou emissor incorreto."""

    pass


class TokenManager:
    """Gerencia geração, assinatura e validação stateless de tokens JWT."""

    def __init__(
        self,
        secret_key: str | None = None,
        algorithm: str = DEFAULT_ALGORITHM,
        issuer: str = DEFAULT_ISSUER,
        expiration_minutes: int | None = None,
    ) -> None:
        settings = get_settings()
        self.secret_key = secret_key or settings.JWT_SECRET_KEY
        self.algorithm = algorithm or settings.JWT_ALGORITHM
        self.issuer = issuer
        self.expiration_minutes = (
            expiration_minutes
            if expiration_minutes is not None
            else settings.JWT_EXPIRATION_MINUTES
        )

    def create_token(
        self,
        user_id: str,
        roles: list[str] | None = None,
        expires_in_seconds: int | None = None,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Gera e assina um novo token JWT com claims padrão."""
        now = int(time.time())
        exp_seconds = (
            expires_in_seconds
            if expires_in_seconds is not None
            else (self.expiration_minutes * 60)
        )
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "roles": roles if roles is not None else ["customer"],
            "iss": self.issuer,
            "iat": now,
            "exp": now + exp_seconds,
            "jti": str(uuid.uuid4()),
        }
        if extra_claims:
            payload.update(extra_claims)

        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def verify_token(self, token: str) -> dict[str, Any]:
        """Decodifica e valida assinatura, expiração e emissor do token JWT."""
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
                issuer=self.issuer,
                options={"require": ["sub", "exp", "iat", "iss"]},
            )
            if not isinstance(claims.get("roles"), list):
                claims["roles"] = ["customer"]
            return claims
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("Token expirado.") from exc
        except jwt.InvalidIssuerError as exc:
            raise InvalidTokenError("Emissor (iss) inválido.") from exc
        except jwt.InvalidSignatureError as exc:
            raise InvalidTokenError("Assinatura do token inválida.") from exc
        except (jwt.DecodeError, jwt.PyJWTError) as exc:
            raise InvalidTokenError(f"Token malformado ou inválido: {exc}") from exc

    def get_user_from_token(self, token: str) -> AuthenticatedUser:
        """Decodifica o token e retorna o objeto AuthenticatedUser correspondente."""
        claims = self.verify_token(token)
        return AuthenticatedUser(
            user_id=str(claims["sub"]),
            roles=list(claims.get("roles", [])),
        )


class LoginRequest(BaseModel):
    username: str = Field(
        default="customer_user",
        description="Identificador do usuário (sub)",
    )
    roles: list[str] = Field(
        default_factory=lambda: ["customer"],
        description="Lista de papéis do usuário (ex: ['customer'], ['admin'])",
    )
    expires_in_seconds: int | None = Field(
        default=None,
        description="Tempo de validade customizado em segundos",
    )


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    roles: list[str]


auth_router = APIRouter(prefix="/auth", tags=["Autenticação"])


@auth_router.post("/login", response_model=LoginResponse)
def login_for_test_token(req: LoginRequest) -> LoginResponse:
    """Endpoint utilitário/mock para emissão de tokens de teste para clientes e admins."""
    token_mgr = TokenManager()
    exp_seconds = req.expires_in_seconds or (token_mgr.expiration_minutes * 60)
    token = token_mgr.create_token(
        user_id=req.username,
        roles=req.roles,
        expires_in_seconds=exp_seconds,
    )
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=exp_seconds,
        user_id=req.username,
        roles=req.roles,
    )
