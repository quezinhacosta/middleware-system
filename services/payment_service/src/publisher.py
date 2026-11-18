import logging
import os
import uuid
from typing import Any

import pika
from pika.exchange_type import ExchangeType

from middleware.transport.publisher import EventEnvelope

logger = logging.getLogger("payment-service.publisher")

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "message-broker")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")

PAYMENTS_EXCHANGE = os.getenv("PAYMENTS_EXCHANGE", "payments")
PRODUCER_SERVICE = "payment-service"
# Filas consumidas pelo notification-service. São declaradas e ligadas aqui para
# que nenhum evento se perca caso o consumidor ainda não tenha subido.
RESULT_QUEUES = ("payment.approved", "payment.rejected")

credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials
)


def build_envelope(event_data: dict[str, Any]) -> EventEnvelope:
    """Monta o envelope padrão. O ``correlation_id`` do pedido sai dos dados e vai
    para o cabeçalho do envelope (gera um novo se não vier)."""
    data = dict(event_data)
    correlation_id = str(data.pop("correlation_id", None) or uuid.uuid4())
    return EventEnvelope(
        event_name=str(data["status"]),
        correlation_id=correlation_id,
        producer_service=PRODUCER_SERVICE,
        data=data,
    )


def publish_event(event_data: dict[str, Any]) -> None:
    """Publica o evento de resultado do pagamento garantindo fechamento da conexão."""
    envelope = build_envelope(event_data)
    connection = pika.BlockingConnection(parameters)
    try:
        channel = connection.channel()
        channel.exchange_declare(
            exchange=PAYMENTS_EXCHANGE,
            # ExchangeType("direct") em vez de ExchangeType.direct: nos stubs do pika
            # os membros do enum estão tipados como `str`, e o mypy rejeitaria.
            exchange_type=ExchangeType("direct"),
            durable=True,
        )
        for queue in RESULT_QUEUES:
            channel.queue_declare(queue=queue, durable=True)
            channel.queue_bind(
                queue=queue, exchange=PAYMENTS_EXCHANGE, routing_key=queue
            )

        channel.basic_publish(
            exchange=PAYMENTS_EXCHANGE,
            routing_key=envelope.event_name,
            body=envelope.model_dump_json().encode(),
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
                message_id=envelope.event_id,
                correlation_id=envelope.correlation_id,
            ),
        )
        logger.info(
            "Evento publicado: %s -> %s",
            envelope.event_name,
            envelope.data.get("payment_id"),
            extra={"correlation_id": envelope.correlation_id},
        )
    finally:
        try:
            if connection.is_open:
                connection.close()
        except Exception:
            pass
