import asyncio
import contextvars
import functools
import inspect
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urlsplit

from .exceptions import MiddlewareTimeoutError
from .timeout_config import Criticality, get_timeout

logger = logging.getLogger("middleware.resilience.timeout")

# Pool limitado: evita criar threads sem controle para funções síncronas.
_executor = ThreadPoolExecutor(
    max_workers=int(os.getenv("TIMEOUT_EXECUTOR_WORKERS", "32")),
    thread_name_prefix="mw-timeout",
)

T = TypeVar("T")


def _resolve_seconds(seconds: float | Criticality) -> float:
    if isinstance(seconds, Criticality):
        return get_timeout(seconds)
    if seconds <= 0:
        raise ValueError("seconds deve ser > 0")
    return float(seconds)


def _sanitize_url(url: str | None) -> str | None:
    """Remove query string e credenciais antes de logar (evita vazar tokens)."""
    if not url:
        return None
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def _extract_url(args: tuple, kwargs: dict) -> str | None:
    candidate = kwargs.get("url")
    if isinstance(candidate, str):
        return candidate
    for arg in args:
        if isinstance(arg, str) and arg.startswith(("http://", "https://")):
            return arg
    return None


def _timeout_error(*, url, limit, start, operation) -> MiddlewareTimeoutError:
    elapsed = time.perf_counter() - start
    logger.error(json.dumps({
        "event": "middleware_timeout",
        "status": 504,
        "url": _sanitize_url(url),
        "operation": operation,
        "timeout_s": limit,
        "duration_ms": round(elapsed * 1000, 1),
    }))
    return MiddlewareTimeoutError(
        url=_sanitize_url(url), timeout=limit, elapsed=elapsed, operation=operation
    )


async def run_with_deadline(
    coro: Awaitable[T], seconds: float, *,
    url: str | None = None, operation: str | None = None,
) -> T:
    """Executa a corrotina com prazo total; ao estourar, ela é cancelada."""
    start = time.perf_counter()
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        raise _timeout_error(
            url=url, limit=seconds, start=start, operation=operation
        ) from None


def with_timeout(seconds: float | Criticality) -> Callable:
    """Decorator de timeout para funções sync e async.

    Uso:
        @with_timeout(1.5)
        @with_timeout(Criticality.PAYMENT)   # lê TIMEOUT_PAYMENT_SECONDS
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        operation = func.__qualname__

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                limit = _resolve_seconds(seconds)
                return await run_with_deadline(
                    func(*args, **kwargs), limit,
                    url=_extract_url(args, kwargs), operation=operation,
                )
            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            limit = _resolve_seconds(seconds)
            start = time.perf_counter()
            ctx = contextvars.copy_context()
            future = _executor.submit(ctx.run, func, *args, **kwargs)
            try:
                return future.result(timeout=limit)
            except FutureTimeoutError:
                if future.done():  # o TimeoutError veio da própria função
                    raise
                future.cancel()
                raise _timeout_error(
                    url=_extract_url(args, kwargs), limit=limit,
                    start=start, operation=operation,
                ) from None
        return sync_wrapper

    return decorator