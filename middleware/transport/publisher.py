"""Produtor de eventos: publica mensagens em envelope padrão no broker.

O código da aplicação depende só de ``EventPublisher``. A biblioteca do
broker (pika) fica encapsulada em ``RabbitMQPublisher``.
"""

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import AMQPError
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventEnvelope(BaseModel):
    """Envelope padrão de todas as mensagens publicadas."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_name: str
    correlation_id: str
    timestamp: str = Field(default_factory=_utc_now)
    producer_service: str
    data: dict[str, Any]


class EventPublisher(ABC):
    """Interface de publicação. Subclasses implementam apenas o transporte."""

    def __init__(self, producer_service: str) -> None:
        self.producer_service = producer_service

    def publish(
        self,
        topic_or_queue: str,
        event_name: str,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> bool:
        """Publica um evento. Retorna False se o broker não aceitou a mensagem.

        Falhas de transporte são registradas e não propagam exceção, para que o
        fluxo principal da requisição não seja interrompido.
        """
        envelope = EventEnvelope(
            event_name=event_name,
            correlation_id=correlation_id,
            producer_service=self.producer_service,
            data=payload,
        )
        try:
            self._transmit(topic_or_queue, envelope.model_dump_json().encode())
        except Exception:
            logger.error(
                "falha ao publicar evento %s em %s",
                event_name,
                topic_or_queue,
                exc_info=True,
                extra={"correlation_id": correlation_id},
            )
            return False
        return True

    @abstractmethod
    def _transmit(self, queue: str, body: bytes) -> None:
        """Envia o corpo da mensagem ao destino. Levanta exceção se falhar."""


class RabbitMQPublisher(EventPublisher):
    """Publica em filas duráveis do RabbitMQ, reutilizando a mesma conexão.

    A conexão é aberta na primeira publicação e reaproveitada nas seguintes.
    Se ela cair, é descartada e reaberta na próxima tentativa.
    """

    def __init__(self, amqp_url: str, producer_service: str, timeout: float = 2.0):
        super().__init__(producer_service)
        self._params = pika.URLParameters(amqp_url)
        self._params.socket_timeout = timeout
        self._params.connection_attempts = 1
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None

    def _get_channel(self) -> BlockingChannel:
        if (
            self._connection is None
            or self._connection.is_closed
            or self._channel is None
            or self._channel.is_closed
        ):
            self._connection = pika.BlockingConnection(self._params)
            self._channel = self._connection.channel()
        return self._channel

    def _reset(self) -> None:
        if self._connection is not None and self._connection.is_open:
            self._connection.close()
        self._connection = None
        self._channel = None

    def _transmit(self, queue: str, body: bytes) -> None:
        try:
            channel = self._get_channel()
            channel.queue_declare(queue=queue, durable=True)
            channel.basic_publish(
                exchange="",
                routing_key=queue,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,  # persistente: sobrevive a restart do broker
                    content_type="application/json",
                ),
            )
        except (AMQPError, OSError):
            self._reset()
            raise
