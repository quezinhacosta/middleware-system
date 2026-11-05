"""Idempotência para consumidores com entrega at-least-once.

O broker pode reenviar a mesma mensagem (ex.: queda de rede antes do ACK). Este módulo
garante que o efeito de negócio (ex.: uma cobrança) aconteça uma única vez por `event_id`.

Protocolo (reserva atômica, não "checa e depois grava"):
  acquire(chave)  -> ACQUIRED    : ninguém viu a chave; reserva por `processing_ttl` e processa
                  -> DONE        : já processada nas últimas 24h; NÃO executa; loga
                                   MESSAGE_ALREADY_PROCESSED; o consumidor dá ACK
                  -> IN_PROGRESS : outra entrega está processando agora; lança
                                   MessageInProgressError (transiente: o EventConsumer
                                   espera o backoff e tenta de novo, já encontrando DONE)
  sucesso do negócio -> complete(chave, ttl=24h)
  falha do negócio   -> release(chave): a retentativa pode reprocessar

Limites conhecidos:
  * Se o processo cair no meio do negócio, a reserva expira após `processing_ttl` e a
    redelivery reprocessa (janela pequena de duplicidade; o negócio ainda deve ser seguro).
  * SQLite ":memory:" perde o histórico ao reiniciar o container. Para o Payment Service,
    use um arquivo em volume (ver docker-compose) ou implemente `IdempotencyStore` com Redis.
  * O relógio é `time.time` (precisa sobreviver a reinícios; `monotonic` não serve).

Uso:
    class PaymentConsumer(EventConsumer):
        idempotency_manager = IdempotencyManager(SqliteIdempotencyStore("/data/idempotency.db"))

        @idempotent(key="event_id", namespace="payments")
        def handle_message(self, envelope: dict) -> None:
            ...  # cobrança: roda no máximo uma vez por event_id
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from middleware.transport.consumer import TransientError, UnrecoverableError

logger = logging.getLogger("middleware.transport.idempotency")

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_PROCESSING_TTL_SECONDS = 300


class IdempotencyConfigError(RuntimeError):
    """Decorator/interceptor usado sem um IdempotencyManager configurado."""


class MessageInProgressError(TransientError):
    """Outra entrega do mesmo evento está em processamento: tente de novo depois."""


class IdempotencyStoreError(TransientError):
    """Registro de idempotência indisponível. Falha fechada: não processa sem garantia."""


class AcquireResult(StrEnum):
    ACQUIRED = "acquired"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class IdempotencyStore(Protocol):
    """Contrato do registro. Implementações precisam de `acquire` atômico."""

    def acquire(self, key: str, lease_ttl: float) -> AcquireResult: ...

    def complete(self, key: str, ttl: float) -> None: ...

    def release(self, key: str) -> None: ...


def _log(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, default=str))


class SqliteIdempotencyStore:
    """Registro em SQLite. `path=":memory:"` para testes/dev; arquivo para persistir."""

    def __init__(
        self, path: str = ":memory:", *, clock: Callable[[], float] = time.time
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()

        # Garante a criação do diretório pai se o caminho for para um ficheiro
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        # autocommit: as transações são controladas à mão (BEGIN IMMEDIATE)
        self._conn = sqlite3.connect(
            path, timeout=5.0, check_same_thread=False, isolation_level=None
        )
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS idempotency ("
                "key TEXT PRIMARY KEY, state TEXT NOT NULL, expires_at REAL NOT NULL)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency(expires_at)"
            )

    def acquire(self, key: str, lease_ttl: float) -> AcquireResult:
        now = self._clock()
        with self._lock:
            # BEGIN IMMEDIATE: trava escrita também entre processos/conexões diferentes
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM idempotency WHERE expires_at <= ?", (now,)
                )
                row = self._conn.execute(
                    "SELECT state FROM idempotency WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO idempotency (key, state, expires_at) VALUES (?, 'processing', ?)",
                        (key, now + lease_ttl),
                    )
                    result = AcquireResult.ACQUIRED
                else:
                    result = (
                        AcquireResult.DONE
                        if row[0] == "done"
                        else AcquireResult.IN_PROGRESS
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return result

    def complete(self, key: str, ttl: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO idempotency (key, state, expires_at) VALUES (?, 'done', ?)",
                (key, self._clock() + ttl),
            )

    def release(self, key: str) -> None:
        with self._lock:
            # só libera reservas em andamento: nunca apaga um evento já concluído
            self._conn.execute(
                "DELETE FROM idempotency WHERE key = ? AND state = 'processing'", (key,)
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class IdempotencyManager:
    """Executa uma função no máximo uma vez por chave, com checagem atômica."""

    def __init__(
        self,
        store: IdempotencyStore,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        processing_ttl_seconds: float = DEFAULT_PROCESSING_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0 or processing_ttl_seconds <= 0:
            raise ValueError("ttl_seconds e processing_ttl_seconds devem ser > 0")
        self._store = store
        self._ttl = ttl_seconds
        self._processing_ttl = processing_ttl_seconds

    def acquire(self, key: str) -> AcquireResult:
        try:
            return self._store.acquire(key, self._processing_ttl)
        except Exception as exc:
            raise IdempotencyStoreError(
                f"registro de idempotência indisponível: {type(exc).__name__}"
            ) from exc

    def complete(self, key: str) -> None:
        self._store.complete(key, self._ttl)

    def release(self, key: str) -> None:
        self._store.release(key)

    def run_once(self, key: str, func: Callable[[], Any]) -> bool:
        """Retorna True se executou `func`, False se a chave já estava concluída."""
        result = self.acquire(key)
        if result is AcquireResult.DONE:
            return False
        if result is AcquireResult.IN_PROGRESS:
            raise MessageInProgressError("evento em processamento por outra entrega")

        try:
            func()
        except BaseException:
            self._release_quietly(key)  # a retentativa precisa poder reprocessar
            raise
        self._complete_quietly(key)
        return True

    def _release_quietly(self, key: str) -> None:
        try:
            self.release(key)
        except Exception as exc:
            _log(
                logging.ERROR,
                "idempotency_release_failed",
                error_type=type(exc).__name__,
            )

    def _complete_quietly(self, key: str) -> None:
        # O negócio já aconteceu: não propagar erro aqui evitaria reprocessar e cobrar de novo.
        try:
            self.complete(key)
        except Exception as exc:
            _log(
                logging.ERROR,
                "idempotency_complete_failed",
                error_type=type(exc).__name__,
            )


class IdempotentConsumerInterceptor:
    """Envolve um handler `handler(envelope)` com deduplicação por `envelope[key]`."""

    def __init__(
        self,
        handler: Callable[[dict], Any],
        manager: IdempotencyManager,
        *,
        key: str = "event_id",
        namespace: str = "default",
    ) -> None:
        self._handler = handler
        self._manager = manager
        self._key = key
        self._namespace = namespace

    def __call__(self, envelope: dict) -> None:
        event_id = self._extract_event_id(envelope)
        executed = self._manager.run_once(
            f"{self._namespace}:{event_id}", lambda: self._handler(envelope)
        )
        if not executed:
            _log(
                logging.WARNING,
                "MESSAGE_ALREADY_PROCESSED",
                namespace=self._namespace,
                event_id=event_id,
            )

    def _extract_event_id(self, envelope: dict) -> str:
        value = envelope.get(self._key)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise UnrecoverableError(
                f"campo '{self._key}' ausente ou inválido no envelope"
            )
        text = str(value).strip()
        if not text:
            raise UnrecoverableError(f"campo '{self._key}' vazio no envelope")
        return text


# ---------- decorator ----------

_default_manager: IdempotencyManager | None = None


def configure_default_manager(manager: IdempotencyManager | None) -> None:
    """Define o manager usado por `@idempotent` quando não há outro (None limpa)."""
    global _default_manager
    _default_manager = manager


def get_default_manager() -> IdempotencyManager | None:
    return _default_manager


def _find_envelope(args: tuple, kwargs: dict) -> dict:
    candidate = kwargs.get("envelope")
    if isinstance(candidate, dict):
        return candidate
    for arg in args:
        if isinstance(arg, dict):
            return arg
    raise TypeError("@idempotent: o handler precisa receber o envelope (dict)")


def _resolve_manager(
    explicit: IdempotencyManager | None, args: tuple[Any, ...]
) -> IdempotencyManager:
    if explicit is not None:
        return explicit
    if args:
        from_instance = getattr(args[0], "idempotency_manager", None)
        if isinstance(from_instance, IdempotencyManager):
            return from_instance
    if _default_manager is not None:
        return _default_manager
    raise IdempotencyConfigError(
        "nenhum IdempotencyManager: passe manager=..., defina `idempotency_manager` "
        "na instância ou chame configure_default_manager()"
    )


def idempotent(
    key: str = "event_id",
    *,
    manager: IdempotencyManager | None = None,
    namespace: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator para `handler(envelope)` ou `método(self, envelope)`.

    Ordem de resolução do manager: argumento `manager`, atributo `idempotency_manager`
    da instância, `configure_default_manager()`.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        ns = namespace or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> None:
            envelope = _find_envelope(args, kwargs)
            mgr = _resolve_manager(manager, args)
            IdempotentConsumerInterceptor(
                lambda _envelope: func(*args, **kwargs), mgr, key=key, namespace=ns
            )(envelope)

        return wrapper

    return decorator
