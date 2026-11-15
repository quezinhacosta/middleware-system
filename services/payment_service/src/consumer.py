import logging
import os
import time

import pika
import pika.exceptions
from src.processor import process_payment
from src.publisher import publish_event

from middleware.transport.consumer import EventConsumer, Topology
from middleware.transport.idempotency import (
    IdempotencyManager,
    SqliteIdempotencyStore,
    idempotent,
)

logger = logging.getLogger("payment-service.consumer")

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "message-broker")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")

credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials
)

QUEUE_NAME = "order.created"
TOPOLOGY = Topology(exchange="orders", queue=QUEUE_NAME, routing_key=QUEUE_NAME)

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
IDEMPOTENCY_DB_PATH = os.getenv("IDEMPOTENCY_DB_PATH", ":memory:")
RECONNECT_DELAY_SECONDS = 5


class PaymentConsumer(EventConsumer):
    """Consome `order.created`: ACK no sucesso, retentativas e DLQ nas falhas,
    e no máximo um processamento financeiro por `event_id`."""

    transient_exceptions = EventConsumer.transient_exceptions + (
        pika.exceptions.AMQPConnectionError,
    )

    def __init__(self, channel, idempotency_manager: IdempotencyManager, **kwargs):
        super().__init__(channel, TOPOLOGY, prefetch_count=1, **kwargs)
        self.idempotency_manager = idempotency_manager

    @idempotent(key="event_id", namespace="payment-service")
    def handle_message(self, envelope: dict) -> None:
        logger.info("Mensagem recebida para o pedido: %s", envelope.get("order_id"))

        result = process_payment(envelope)
        publish_event(result)

        logger.info("Pagamento processado para o pedido: %s", result["order_id"])


def _close_quietly(connection) -> None:
    try:
        if connection is not None and connection.is_open:
            connection.close()
    except Exception:
        pass


def start_consuming():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manager = IdempotencyManager(SqliteIdempotencyStore(IDEMPOTENCY_DB_PATH))

    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    conn_params = pika.ConnectionParameters(
        host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials
    )

    while True:
        connection = None
        try:
            logger.info("Tentando conectar ao RabbitMQ em %s...", RABBITMQ_HOST)
            connection = pika.BlockingConnection(conn_params)
            consumer = PaymentConsumer(
                connection.channel(), manager, max_retries=MAX_RETRIES
            )

            logger.info("Aguardando mensagens. Para sair pressione CTRL+C")
            consumer.start()

        except pika.exceptions.AMQPConnectionError:
            logger.warning(
                "Falha na conexão com o RabbitMQ. Tentando novamente em 5 segundos..."
            )
        except pika.exceptions.AMQPChannelError as exc:
            logger.error(
                "RabbitMQ recusou a declaração/consumo: %s. Nova tentativa em 5 segundos...",
                exc,
            )
        except KeyboardInterrupt:
            logger.info("Consumidor interrompido pelo usuário.")
            break
        finally:
            _close_quietly(connection)

        time.sleep(RECONNECT_DELAY_SECONDS)
