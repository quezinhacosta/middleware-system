import json
import logging
import os
from typing import Any

import pika

logger = logging.getLogger("payment-service.publisher")

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "message-broker")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")

credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials
)


def publish_event(event_data: dict[str, Any]) -> None:
    """Publica o evento de resultado do pagamento no RabbitMQ garantindo fechamento da conexão."""
    connection = pika.BlockingConnection(parameters)
    try:
        channel = connection.channel()
        status_queue = str(event_data["status"])

        channel.queue_declare(queue=status_queue, durable=True)

        channel.basic_publish(
            exchange="",
            routing_key=status_queue,
            body=json.dumps(event_data),
            properties=pika.BasicProperties(
                delivery_mode=2,
            ),
        )
        logger.info(
            "Evento publicado: %s -> %s", status_queue, event_data.get("payment_id")
        )
    finally:
        try:
            if connection.is_open:
                connection.close()
        except Exception:
            pass
