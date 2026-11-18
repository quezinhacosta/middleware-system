import logging
import os
import threading
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
IDEMPOTENCY_NAMESPACE = "payment-service"


class PaymentConsumer(EventConsumer):
    """Consome `order.created`: ACK no sucesso, retentativas e DLQ nas falhas,
    e no máximo um processamento financeiro por `event_id`."""

    transient_exceptions = EventConsumer.transient_exceptions + (
        pika.exceptions.AMQPConnectionError,
    )

    def __init__(self, channel, idempotency_manager: IdempotencyManager, **kwargs):
        super().__init__(channel, TOPOLOGY, prefetch_count=1, **kwargs)
        self.idempotency_manager = idempotency_manager

    @idempotent(key="event_id", namespace=IDEMPOTENCY_NAMESPACE)
    def handle_message(self, envelope: dict) -> None:
        order_data = extract_order_data(envelope)
        logger.info("Mensagem recebida para o pedido: %s", order_data.get("order_id"))
        process_and_publish(order_data)


def extract_order_data(envelope: dict) -> dict:
    """Aceita o envelope padrão do middleware (dados em ``data``) e o formato plano
    enviado pelo fallback do order-service."""
    data = envelope.get("data")
    if not isinstance(data, dict):
        return envelope
    order_data = dict(data)
    if envelope.get("correlation_id") and not order_data.get("correlation_id"):
        order_data["correlation_id"] = envelope["correlation_id"]
    return order_data


def process_and_publish(order_data: dict) -> dict:
    """Processa o pagamento e publica o resultado. Usado pelo consumidor da fila e
    pelo endpoint síncrono ``POST /payments``."""
    result: dict = process_payment(order_data)
    publish_event(result)
    logger.info(
        "Pagamento processado para o pedido %s: %s",
        result["order_id"],
        result["status"],
    )
    return result


_manager: IdempotencyManager | None = None
_manager_lock = threading.Lock()


def get_idempotency_manager() -> IdempotencyManager:
    """Registro de idempotência único do processo: fila e HTTP compartilham o mesmo
    store, então um pedido nunca é cobrado duas vezes, venha por onde vier."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = IdempotencyManager(SqliteIdempotencyStore(IDEMPOTENCY_DB_PATH))
        return _manager


def _close_quietly(connection) -> None:
    try:
        if connection is not None and connection.is_open:
            connection.close()
    except Exception:
        pass


def start_consuming():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manager = get_idempotency_manager()

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
