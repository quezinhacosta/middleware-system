"""Camada de transporte do middleware (HTTP e mensageria)."""

from .consumer import (
    EventConsumer,
    Topology,
    TransientError,
    UnrecoverableError,
    declare_topology,
)
from .http_client import (
    DistributedHttpClient,
    GatewayTimeoutException,
    ServiceUnavailableException,
)
from .idempotency import (
    IdempotencyManager,
    IdempotentConsumerInterceptor,
    SqliteIdempotencyStore,
    idempotent,
)

__all__ = [
    "DistributedHttpClient",
    "GatewayTimeoutException",
    "ServiceUnavailableException",
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
