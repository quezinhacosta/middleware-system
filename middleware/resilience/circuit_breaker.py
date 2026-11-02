from __future__ import annotations

import functools
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

try:  # métrica é opcional: sem prometheus_client o breaker continua funcionando
    from prometheus_client import Gauge

    _STATE_GAUGE: Any = Gauge(
        "circuit_breaker_state",
        "Estado do circuit breaker (0=CLOSED, 1=HALF_OPEN, 2=OPEN)",
        ["name"],
    )
except (ImportError, ValueError):  # ValueError: registro duplicado (reload)
    _STATE_GAUGE = None


class CircuitState(IntEnum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


class CircuitBreakerOpenException(Exception):
    """Lançada quando o circuito rejeita a chamada sem tocar a rede."""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = max(retry_after, 0.0)
        super().__init__(
            f"Circuito '{name}' aberto; nova tentativa em ~{self.retry_after:.1f}s"
        )


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 10.0,
        expected_exceptions: tuple[type[BaseException], ...] = (Exception,),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold deve ser >= 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout deve ser > 0")

        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._expected = expected_exceptions
        self._clock = clock  # injetável para testes determinísticos

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._canary_in_flight = False
        self._publish_metric()

    # API
    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        is_canary = self._before_call()
        try:
            result = func(*args, **kwargs)
        except self._expected:
            self._on_failure(is_canary)
            raise
        except Exception:
            # O serviço respondeu (ex.: erro de negócio): não é falha de infra.
            self._on_success(is_canary)
            raise
        except BaseException:
            # Cancelamento / KeyboardInterrupt: não diz nada sobre o serviço.
            self._on_abort(is_canary)
            raise
        self._on_success(is_canary)
        return result

    async def call_async(
        self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        is_canary = self._before_call()
        try:
            result = await func(*args, **kwargs)
        except self._expected:
            self._on_failure(is_canary)
            raise
        except Exception:
            self._on_success(is_canary)
            raise
        except BaseException:  # inclui asyncio.CancelledError
            self._on_abort(is_canary)
            raise
        self._on_success(is_canary)
        return result

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

    # transições (lock)
    def _before_call(self) -> bool:
        # Decide se a chamada passa. Retorna True se for a requisição canário
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return False

            if self._state is CircuitState.OPEN:
                elapsed = self._clock() - self._opened_at
                if elapsed < self._recovery_timeout:
                    raise CircuitBreakerOpenException(
                        self.name, self._recovery_timeout - elapsed
                    )
                self._transition(CircuitState.HALF_OPEN)
                self._canary_in_flight = True
                return True

            # HALF_OPEN: só um canário por vez
            if self._canary_in_flight:
                raise CircuitBreakerOpenException(self.name, self._recovery_timeout)
            self._canary_in_flight = True
            return True

    def _on_success(self, is_canary: bool) -> None:
        with self._lock:
            if is_canary:
                self._canary_in_flight = False
                self._failures = 0
                self._transition(CircuitState.CLOSED)
            elif self._state is CircuitState.CLOSED:
                self._failures = 0  # falhas contadas são "consecutivas"
            # chamada atrasada que começou em CLOSED e terminou com o circuito
            # em outro estado: resultado ignorado

    def _on_failure(self, is_canary: bool) -> None:
        with self._lock:
            if is_canary:
                self._canary_in_flight = False
                self._open()
            elif self._state is CircuitState.CLOSED:
                self._failures += 1
                if self._failures >= self._failure_threshold:
                    self._open()

    def _on_abort(self, is_canary: bool) -> None:
        with self._lock:
            if is_canary:
                # Devolve o circuito a OPEN mantendo opened_at: o próximo
                # chamador vira o novo canário imediatamente
                self._canary_in_flight = False
                self._transition(CircuitState.OPEN)

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._transition(CircuitState.OPEN)

    def _transition(self, new_state: CircuitState) -> None:
        if new_state is self._state:
            return
        logger.warning(
            "circuit_breaker=%s transition=%s->%s failures=%d",
            self.name, self._state.name, new_state.name, self._failures,
        )
        self._state = new_state
        self._publish_metric()

    def _publish_metric(self) -> None:
        if _STATE_GAUGE is not None:
            _STATE_GAUGE.labels(name=self.name).set(int(self._state))
