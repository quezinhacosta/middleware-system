"""Camada de transporte do middleware (mensageria)."""
from .consumer import (
    EventConsumer,
    Topology,
    TransientError,
    UnrecoverableError,
    declare_topology,
)
from .idempotency import (
    IdempotencyManager,
    IdempotentConsumerInterceptor,
    SqliteIdempotencyStore,
    idempotent,
)

__all__ = [
    "EventConsumer",
    "IdempotencyManager",
    "IdempotentConsumerInterceptor",
    "SqliteIdempotencyStore",
    "Topology",
    "TransientError",
    "UnrecoverableError",
    "declare_topology",
    "idempotent",
]