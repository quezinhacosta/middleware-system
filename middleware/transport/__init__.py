"""Camada de transporte do middleware (mensageria)."""

from .consumer import (
    EventConsumer,
    Topology,
    TransientError,
    UnrecoverableError,
    declare_topology,
)

__all__ = [
    "EventConsumer",
    "Topology",
    "TransientError",
    "UnrecoverableError",
    "declare_topology",
]
