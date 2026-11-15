"""Retentativa com backoff exponencial e jitter.

Atraso após a tentativa `attempt` (1 = a primeira, que acabou de falhar):

    base   = min(max_delay, base_delay * 2 ** (attempt - 1))
    sleep  = base + jitter,   jitter = base * jitter_ratio * random()   (uniforme em [0, ratio*base])

Com os padrões (base 0.5s, máx. 5s, jitter 25%): ~0.5s, ~1s, ~2s, ~4s, 5s...
O jitter é somado depois do teto, então o atraso pode passar de `max_delay` em até
`jitter_ratio`. O jitter evita que vários clientes retentem no mesmo milissegundo.

`max_attempts` conta as tentativas TOTAIS (a inicial + as retentativas).

O que é retentado (e só isso):
  * exceções transitórias: ConnectionError, TimeoutError, MiddlewareTimeoutError e
    httpx.TransportError (queda de conexão, timeouts de fase);
  * respostas com status 502/503/504 e httpx.HTTPStatusError com esses status.
Erros 4xx (400, 401, 404...) e qualquer outra exceção falham na hora, sem retentativa;
`retryable_status_codes` só aceita códigos 5xx. CircuitBreakerOpenException também não é
retentada (com o circuito aberto, insistir só atrapalha a recuperação).

Ao esgotar as tentativas: exceções são relançadas; respostas HTTP retentáveis (ex.: 503)
são devolvidas, como o DistributedHttpClient faz com status de erro.

Cuidado: retentar uma operação NÃO idempotente (ex.: um POST de cobrança) após timeout ou
queda de conexão pode duplicá-la, pois a 1ª tentativa pode ter chegado ao destino. Só use
em operações idempotentes ou com chave de idempotência.

Uso (síncrono, assíncrono e decorator; ordem recomendada com circuit breaker:
retry por fora, breaker por dentro, para cada tentativa contar no circuito):

    policy = RetryPolicy(max_attempts=3, base_delay=0.5, max_delay=5.0)

    resposta = await policy.call_async(client.get_catalog, url)
    resultado = policy.call(requests_get, url)

    @RetryPolicy()
    def consultar(): ...
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

import httpx

from middleware.resilience.exceptions import MiddlewareTimeoutError

logger = logging.getLogger("middleware.resilience.retry")

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 5.0
DEFAULT_JITTER_RATIO = 0.25
DEFAULT_RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})
DEFAULT_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    MiddlewareTimeoutError,
    httpx.TransportError,
)


def _log(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, default=str))


def _operation_name(func: Callable[..., Any]) -> str:
    return (
        getattr(func, "__qualname__", None)
        or getattr(func, "__name__", None)
        or type(func).__name__
    )


def _exception_reason(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"status_{exc.response.status_code}"
    return type(exc).__name__


class RetryPolicy:
    """Política de retentativa com backoff exponencial e jitter (sync, async e decorator)."""

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        jitter_ratio: float = DEFAULT_JITTER_RATIO,
        retryable_exceptions: tuple[
            type[BaseException], ...
        ] = DEFAULT_RETRYABLE_EXCEPTIONS,
        retryable_status_codes: Iterable[int] = DEFAULT_RETRYABLE_STATUS_CODES,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_func: Callable[[], float] = random.random,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts deve ser >= 1")
        if base_delay < 0 or max_delay < 0:
            raise ValueError("base_delay e max_delay devem ser >= 0")
        if max_delay < base_delay:
            raise ValueError("max_delay deve ser >= base_delay")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter_ratio deve estar entre 0 e 1")
        codes = frozenset(retryable_status_codes)
        invalid = sorted(code for code in codes if not 500 <= code <= 599)
        if invalid:
            raise ValueError(
                f"retryable_status_codes só aceita códigos 5xx (nunca 4xx): {invalid}"
            )

        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._jitter_ratio = jitter_ratio
        self._exceptions = retryable_exceptions
        self._status_codes = codes
        self._sleep = sleep  # injetáveis: testes sem espera real e sem aleatoriedade
        self._async_sleep = async_sleep
        self._random = random_func

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def base_delay(self) -> float:
        return self._base_delay

    @property
    def max_delay(self) -> float:
        return self._max_delay

    # ---------- atraso ----------

    def compute_delay(self, attempt: int) -> float:
        """Atraso (s) antes da próxima tentativa; `attempt` = nº da que acabou de falhar."""
        if attempt < 1:
            raise ValueError("attempt deve ser >= 1")
        exponent = min(attempt - 1, 62)  # evita estouro de float em tentativas absurdas
        capped = min(self._max_delay, self._base_delay * (2**exponent))
        jitter = capped * self._jitter_ratio * self._random()
        return float(capped + jitter)

    # ---------- execução ----------

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        name = _operation_name(func)
        for attempt in range(1, self._max_attempts + 1):
            error: Exception | None = None
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                if not self._is_retryable_exception(exc):
                    self._log_not_retryable(name, attempt, _exception_reason(exc))
                    raise
                error, reason = exc, _exception_reason(exc)
            else:
                retry_reason = self._classify_result(name, attempt, result)
                if retry_reason is None:
                    return result
                reason = retry_reason

            delay = self._next_delay(name, attempt, reason)
            if delay is None:
                if error is not None:
                    raise error
                return result
            self._sleep(delay)
        raise AssertionError("inalcançável")

    async def call_async(
        self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        name = _operation_name(func)
        for attempt in range(1, self._max_attempts + 1):
            error: Exception | None = None
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:  # CancelledError não é Exception: propaga
                if not self._is_retryable_exception(exc):
                    self._log_not_retryable(name, attempt, _exception_reason(exc))
                    raise
                error, reason = exc, _exception_reason(exc)
            else:
                retry_reason = self._classify_result(name, attempt, result)
                if retry_reason is None:
                    return result
                reason = retry_reason

            delay = self._next_delay(name, attempt, reason)
            if delay is None:
                if error is not None:
                    raise error
                return result
            await self._async_sleep(delay)
        raise AssertionError("inalcançável")

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        # Uso como decorador, para funções sync ou async
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await self.call_async(func, *args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.call(func, *args, **kwargs)

        return wrapper

    # ---------- internos ----------

    def _is_retryable_exception(self, exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in self._status_codes
        return isinstance(exc, self._exceptions)

    def _classify_result(self, name: str, attempt: int, result: Any) -> str | None:
        """Devolve o motivo se a resposta pede retentativa; senão registra e devolve None."""
        status = getattr(result, "status_code", None)
        if isinstance(status, int) and status in self._status_codes:
            return f"status_{status}"
        if isinstance(status, int) and status >= 400:
            self._log_not_retryable(name, attempt, f"status_{status}")
        elif attempt > 1:
            _log(logging.INFO, "retry_succeeded", operation=name, attempts=attempt)
        return None

    def _next_delay(self, name: str, attempt: int, reason: str) -> float | None:
        """Loga e devolve o atraso até a próxima tentativa; None se as tentativas acabaram."""
        if attempt >= self._max_attempts:
            _log(
                logging.ERROR,
                "retry_exhausted",
                operation=name,
                attempts=attempt,
                reason=reason,
            )
            return None
        delay = self.compute_delay(attempt)
        _log(
            logging.WARNING,
            "retry_attempt",
            operation=name,
            attempt=attempt,
            max_attempts=self._max_attempts,
            delay_s=round(delay, 3),
            reason=reason,
        )
        return delay

    @staticmethod
    def _log_not_retryable(name: str, attempt: int, reason: str) -> None:
        _log(
            logging.INFO,
            "retry_not_retryable",
            operation=name,
            attempt=attempt,
            reason=reason,
        )
