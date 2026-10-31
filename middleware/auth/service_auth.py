"""Autenticação Service-to-Service (S2S) — princípio Zero Trust.

Fluxo:
  Order Service --(X-Internal-Service-Key: <JWT HS256 curto>)--> Catalog Service
  O receptor valida assinatura, expiração, audiência (aud) e emissor (iss).

Variáveis de ambiente:
  INTERNAL_SERVICE_SECRET  segredo compartilhado (>= 32 bytes). Obrigatório.
  INTERNAL_TOKEN_TTL       validade do token em segundos (padrão: 60).
"""
from __future__ import annotations

import os
import threading  
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

import jwt

SERVICE_KEY_HEADER = "X-Internal-Service-Key"  
ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 60
LEEWAY_SECONDS = 5
MIN_SECRET_BYTES = 32
REFRESH_MARGIN_SECONDS = 10  

class ServiceAuthConfigError(RuntimeError):
    """Configuração inválida (ex.: segredo ausente ou fraco). Falha rápido no startup."""


class ServiceAuthError(Exception):
    """Token ausente, inválido, expirado ou com audiência errada."""


class ServiceForbiddenError(ServiceAuthError):
    """Token válido, mas o serviço chamador não tem permissão para este endpoint."""


@dataclass(frozen=True)
class ServiceIdentity:
    service: str
    token_id: str


class ServiceAuthenticator:
    """Gera e valida tokens S2S. Stateless e thread-safe."""

    def __init__(self, secret: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        if not secret or len(secret.encode()) < MIN_SECRET_BYTES:
            raise ServiceAuthConfigError(
                f"INTERNAL_SERVICE_SECRET deve ter pelo menos {MIN_SECRET_BYTES} bytes"
            )
        self._secret = secret
        self.ttl_seconds = ttl_seconds

    def generate_token(self, issuer: str, audience: str) -> tuple[str, int]:
        """Retorna (token, exp_epoch). `issuer` = quem chama; `audience` = quem recebe."""
        now = int(time.time())
        exp = now + self.ttl_seconds
        claims = {
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": exp,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self._secret, algorithm=ALGORITHM), exp

    def validate_token(
        self,
        token: str,
        audience: str,
        allowed_issuers: Iterable[str] | None = None,
    ) -> ServiceIdentity:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],  # nunca aceitar "none" ou outros algoritmos
                audience=audience,
                leeway=LEEWAY_SECONDS,
                options={"require": ["iss", "aud", "exp", "iat", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise ServiceAuthError(type(exc).__name__) from exc

        issuer = claims["iss"]
        if allowed_issuers is not None and issuer not in set(allowed_issuers):
            raise ServiceForbiddenError(f"issuer não autorizado: {issuer}")
        return ServiceIdentity(service=issuer, token_id=claims["jti"])


class ServiceTokenProvider:
    """Lado cliente: mantém token em cache por audiência para não assinar a cada chamada."""

    def __init__(self, service_name: str, authenticator: ServiceAuthenticator) -> None:
        self._service_name = service_name
        self._auth = authenticator
        self._cache: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def get_token(self, audience: str) -> str:
        now = time.time()
        with self._lock:
            cached = self._cache.get(audience)
            if cached and cached[1] - now > REFRESH_MARGIN_SECONDS:
                return cached[0]
            token, exp = self._auth.generate_token(self._service_name, audience)
            self._cache[audience] = (token, exp)
            return token

    def auth_headers(self, audience: str) -> dict[str, str]:
        return {SERVICE_KEY_HEADER: self.get_token(audience)}

    
@lru_cache(maxsize=1)
def get_authenticator() -> ServiceAuthenticator:
    """Singleton lido do ambiente. Chame no startup para falhar rápido."""
    return ServiceAuthenticator(
        secret=os.environ.get("INTERNAL_SERVICE_SECRET", ""),
        ttl_seconds=int(os.environ.get("INTERNAL_TOKEN_TTL", DEFAULT_TTL_SECONDS)),
    )