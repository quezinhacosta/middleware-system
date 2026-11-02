import time

import httpx

from middleware.resilience.exceptions import MiddlewareTimeoutError
from middleware.resilience.timeout import _sanitize_url, run_with_deadline
from middleware.resilience.timeout_config import Criticality, get_timeout


class DistributedHttpClient:
    """Client HTTP interno para chamadas entre serviços.

    Versão mínima: timeout por criticidade. Pontos de extensão futuros
    (auth S2S, circuit breaker) entram via `default_headers` e wrappers.
    """

    def __init__(
        self,
        *,
        default_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,   # injetável para testes
        max_connections: int = 100,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            headers=default_headers,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=20,
            ),
            follow_redirects=False,  # evita seguir redirect para host inesperado
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        criticality: Criticality = Criticality.DEFAULT,
        **kwargs,
    ) -> httpx.Response:
        limit = get_timeout(criticality)
        start = time.perf_counter()
        try:
            # prazo TOTAL (wait_for cancela a task e fecha o socket)
            # + timeout por fase do httpx (connect/read/write/pool)
            return await run_with_deadline(
                self._client.request(method, url, timeout=limit, **kwargs),
                limit,
                url=url,
                operation=f"{method} DistributedHttpClient",
            )
        except httpx.TimeoutException:
            raise MiddlewareTimeoutError(
                url=_sanitize_url(url),  # remove query string e credenciais
                timeout=limit,
                elapsed=time.perf_counter() - start,
            ) from None

    async def get_catalog(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, criticality=Criticality.CATALOG, **kwargs)

    async def authorize_payment(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, criticality=Criticality.PAYMENT, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "DistributedHttpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()
