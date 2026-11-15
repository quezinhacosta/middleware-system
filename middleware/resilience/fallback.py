"""Mecanismo de Fallback para degradação graciosa em falhas de serviços dependentes.

Intercepta falhas irrecuperáveis de infraestrutura (CircuitBreakerOpenException,
MiddlewareTimeoutError, httpx.HTTPError, ConnectionError, TimeoutError, OSError)
e aciona um manipulador alternativo (`fallback_handler`), emitindo log estruturado
com a tag `FALLBACK_TRIGGERED`.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

import httpx

from middleware.resilience.circuit_breaker import CircuitBreakerOpenException
from middleware.resilience.exceptions import MiddlewareTimeoutError

logger = logging.getLogger("middleware.resilience.fallback")

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])

DEFAULT_FALLBACK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    CircuitBreakerOpenException,
    MiddlewareTimeoutError,
    httpx.HTTPError,
    ConnectionError,
    TimeoutError,
    OSError,
)


def _log_fallback(operation: str, exc: BaseException) -> None:
    payload = {
        "event": "FALLBACK_TRIGGERED",
        "tag": "FALLBACK_TRIGGERED",
        "operation": operation,
        "reason": type(exc).__name__,
        "detail": str(exc),
    }
    logger.warning(json.dumps(payload, default=str))


def _operation_name(func: Callable[..., Any]) -> str:
    return (
        getattr(func, "__qualname__", None)
        or getattr(func, "__name__", None)
        or type(func).__name__
    )


def _accepts_args(handler: Callable[..., Any]) -> bool:
    """Verifica se o fallback_handler aceita argumentos ou se é uma função sem parâmetros."""
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        return True
    return len(sig.parameters) > 0


def _invoke_sync_handler(handler: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    if _accepts_args(handler):
        return handler(*args, **kwargs)
    return handler()


async def _invoke_async_handler(
    handler: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    if _accepts_args(handler):
        result = handler(*args, **kwargs)
    else:
        result = handler()
    if inspect.isawaitable(result):
        return await result
    return result


class FallbackPolicy:
    """Política de execução com fallback (uso direto via `.call`/`.call_async` ou decorator)."""

    def __init__(
        self,
        fallback_handler: Callable[..., Any],
        *,
        exceptions: tuple[type[BaseException], ...] = DEFAULT_FALLBACK_EXCEPTIONS,
    ) -> None:
        if not callable(fallback_handler):
            raise TypeError("fallback_handler deve ser um objeto chamável (callable)")
        if not exceptions:
            raise ValueError("exceptions não pode ser vazio")
        self._handler = fallback_handler
        self._exceptions = exceptions

    @property
    def fallback_handler(self) -> Callable[..., Any]:
        return self._handler

    @property
    def exceptions(self) -> tuple[type[BaseException], ...]:
        return self._exceptions

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        name = _operation_name(func)
        try:
            return func(*args, **kwargs)
        except self._exceptions as exc:
            _log_fallback(name, exc)
            return _invoke_sync_handler(self._handler, *args, **kwargs)

    async def call_async(
        self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
    ) -> Any:
        name = _operation_name(func)
        try:
            return await func(*args, **kwargs)
        except self._exceptions as exc:
            _log_fallback(name, exc)
            return await _invoke_async_handler(self._handler, *args, **kwargs)

    def __call__(self, func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await self.call_async(func, *args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.call(func, *args, **kwargs)

        return cast(F, wrapper)


def with_fallback(
    fallback_handler: Callable[..., Any],
    *,
    exceptions: tuple[type[BaseException], ...] = DEFAULT_FALLBACK_EXCEPTIONS,
) -> FallbackPolicy:
    """Decorator `@with_fallback(fallback_handler)` para degradação graciosa."""
    return FallbackPolicy(fallback_handler, exceptions=exceptions)
