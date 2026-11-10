"""Cliente HTTP distribuído para comunicação inter-serviços.

Garante validação de contratos, injeção automática de telemetria (X-Correlation-ID),
repasses de autorização do contexto da requisição original, e mapeamento de erros
de transporte/socket para exceções padronizadas do middleware.
"""

import time
from typing import Any

import httpx

from middleware.auth.context import get_auth_token
from middleware.auth.service_auth import SERVICE_KEY_HEADER, ServiceTokenProvider
from middleware.observability.correlation import CORRELATION_HEADER, get_correlation_id
from middleware.resilience.exceptions import (
    GatewayTimeoutException,
    MiddlewareTimeoutError,
    ServiceUnavailableException,
)
from middleware.resilience.timeout import _sanitize_url, run_with_deadline
from middleware.resilience.timeout_config import Criticality, get_timeout


class DistributedHttpClient:
    """Cliente HTTP interno para comunicação síncrona RPC entre microsserviços.

    Implementa o padrão de comunicação síncrona ponto a ponto (Request-Response)
    da arquitetura distribuída através de corrotinas assíncronas não-bloqueantes
    (httpx.AsyncClient), garantindo alta taxa de transferência e resiliência.

    Recursos fornecidos:
    - Métodos semânticos get, post, put, delete e request genérico.
    - Injeção automática de cabeçalhos de rastreamento (X-Correlation-ID).
    - Injeção automática de credenciais do contexto da requisição (Authorization).
    - Injeção de credenciais de serviço interno (S2S) via ServiceTokenProvider.
    - Mapeamento de falhas de transporte/socket em ServiceUnavailableException e GatewayTimeoutException.
    - Validação de payload para prevenir ambiguidades (conflito json vs data).
    - Suporte a controle de timeout por criticidade.
    """

    def __init__(
        self,
        *,
        default_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        max_connections: int = 100,
        service_token_provider: ServiceTokenProvider | None = None,
    ) -> None:
        self._service_token_provider = service_token_provider
        self._client = client or httpx.AsyncClient(
            headers=default_headers,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=20,
            ),
            follow_redirects=False,
        )

    def _prepare_headers(
        self,
        headers: dict[str, str] | httpx.Headers | None = None,
        target_audience: str | None = None,
    ) -> httpx.Headers:
        """Injeta automaticamente headers de rastreamento e autorização do contexto."""
        prepared = httpx.Headers(headers or {})

        # Injeta X-Correlation-ID do contexto se não explicitado
        correlation_id = get_correlation_id()
        if correlation_id and CORRELATION_HEADER not in prepared:
            prepared[CORRELATION_HEADER] = correlation_id

        # Injeta Authorization do contexto se não explicitado
        auth_token = get_auth_token()
        if auth_token and "Authorization" not in prepared:
            header_value = (
                auth_token
                if auth_token.lower().startswith("bearer ")
                else f"Bearer {auth_token}"
            )
            prepared["Authorization"] = header_value

        # Injeta token de serviço interno (S2S) se provedor e audiência configurados
        if self._service_token_provider and target_audience:
            if SERVICE_KEY_HEADER not in prepared:
                prepared[SERVICE_KEY_HEADER] = self._service_token_provider.get_token(
                    target_audience
                )

        return prepared

    async def request(
        self,
        method: str,
        url: str,
        criticality: Criticality = Criticality.DEFAULT,
        headers: dict[str, str] | httpx.Headers | None = None,
        target_audience: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Executa requisição HTTP com mapeamento de falhas e prazos de resiliência."""
        limit = get_timeout(criticality)
        start = time.perf_counter()
        req_headers = self._prepare_headers(headers, target_audience=target_audience)

        try:
            return await run_with_deadline(
                self._client.request(
                    method, url, timeout=limit, headers=req_headers, **kwargs
                ),
                limit,
                url=url,
                operation=f"{method} DistributedHttpClient",
            )
        except (
            httpx.TimeoutException,
            MiddlewareTimeoutError,
            TimeoutError,
        ) as exc:
            elapsed = time.perf_counter() - start
            raise GatewayTimeoutException(
                url=_sanitize_url(url),
                timeout=limit,
                elapsed=elapsed,
                operation=f"{method} DistributedHttpClient",
            ) from exc
        except (
            httpx.ConnectError,
            httpx.NetworkError,
            ConnectionError,
            OSError,
        ) as exc:
            sanitized = _sanitize_url(url)
            raise ServiceUnavailableException(
                f"Serviço inacessível ou conexão recusada em {sanitized}: {exc}",
                url=sanitized,
            ) from exc

    async def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | httpx.Headers | None = None,
        criticality: Criticality = Criticality.DEFAULT,
        target_audience: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Executa requisição HTTP GET com injeção de contexto e validação."""
        return await self.request(
            "GET",
            url,
            criticality=criticality,
            headers=headers,
            params=params,
            target_audience=target_audience,
            **kwargs,
        )

    async def post(
        self,
        url: str,
        json: Any = None,
        data: Any = None,
        headers: dict[str, str] | httpx.Headers | None = None,
        criticality: Criticality = Criticality.DEFAULT,
        target_audience: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Executa requisição HTTP POST com payload JSON ou form data."""
        if json is not None and data is not None:
            raise ValueError(
                "Não é permitido enviar 'json' e 'data' simultaneamente na mesma requisição."
            )
        return await self.request(
            "POST",
            url,
            criticality=criticality,
            headers=headers,
            json=json,
            data=data,
            target_audience=target_audience,
            **kwargs,
        )

    async def put(
        self,
        url: str,
        json: Any = None,
        data: Any = None,
        headers: dict[str, str] | httpx.Headers | None = None,
        criticality: Criticality = Criticality.DEFAULT,
        target_audience: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Executa requisição HTTP PUT com atualização de recurso."""
        if json is not None and data is not None:
            raise ValueError(
                "Não é permitido enviar 'json' e 'data' simultaneamente na mesma requisição."
            )
        return await self.request(
            "PUT",
            url,
            criticality=criticality,
            headers=headers,
            json=json,
            data=data,
            target_audience=target_audience,
            **kwargs,
        )

    async def delete(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | httpx.Headers | None = None,
        criticality: Criticality = Criticality.DEFAULT,
        target_audience: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Executa requisição HTTP DELETE para remoção de recurso."""
        return await self.request(
            "DELETE",
            url,
            criticality=criticality,
            headers=headers,
            params=params,
            target_audience=target_audience,
            **kwargs,
        )

    async def get_catalog(self, url: str, **kwargs: Any) -> httpx.Response:
        """Atalho semântico para chamadas ao catálogo."""
        kwargs.setdefault("target_audience", "catalog-service")
        return await self.get(url, criticality=Criticality.CATALOG, **kwargs)

    async def authorize_payment(self, url: str, **kwargs: Any) -> httpx.Response:
        """Atalho semântico para autorizações de pagamento."""
        kwargs.setdefault("target_audience", "payment-service")
        return await self.post(url, criticality=Criticality.PAYMENT, **kwargs)

    async def aclose(self) -> None:
        """Encerra conexões abertas no pool HTTP."""
        await self._client.aclose()

    async def __aenter__(self) -> "DistributedHttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
