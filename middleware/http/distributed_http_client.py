"""Módulo de compatibilidade para DistributedHttpClient.

Re-exporta a implementação oficial localizada em middleware.transport.http_client.
"""

from middleware.transport.http_client import (
    DistributedHttpClient,
    GatewayTimeoutException,
    ServiceUnavailableException,
)

__all__ = [
    "DistributedHttpClient",
    "GatewayTimeoutException",
    "ServiceUnavailableException",
]
