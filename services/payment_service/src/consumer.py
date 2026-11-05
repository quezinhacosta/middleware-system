import logging
import os
import time

import pika
from src.processor import process_payment
from src.publisher import publish_event

from middleware.transport.consumer import EventConsumer, Topology
from middleware.transport.idempotency import (
    IdempotencyManager,
    SqliteIdempotencyStore,
    idempotent,
)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "message-broker")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", 5672))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")

credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials
)

QUEUE_NAME = "order.created"
# DLX: orders.dlx | DLQ: order.created.dlq (declaradas por declare_topology no setup)
TOPOLOGY = Topology(exchange="orders", queue=QUEUE_NAME, routing_key=QUEUE_NAME)

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
# Arquivo em volume para sobreviver a reinícios; ":memory:" só para dev/testes.
IDEMPOTENCY_DB_PATH = os.getenv("IDEMPOTENCY_DB_PATH", ":memory:")
RECONNECT_DELAY_SECONDS = 5


class PaymentConsumer(EventConsumer):
    """Consome `order.created`: ACK no sucesso, retentativas e DLQ nas falhas,
    e no máximo um processamento financeiro por `event_id`."""

    # Falha ao publicar o resultado (broker indisponível) também é transiente.
    transient_exceptions = EventConsumer.transient_exceptions + (
        pika.exceptions.AMQPConnectionError,
    )

    def __init__(self, channel, idempotency_manager: IdempotencyManager, **kwargs):
        super().__init__(channel, TOPOLOGY, prefetch_count=1, **kwargs)
        self.idempotency_manager = idempotency_manager

    @idempotent(key="event_id", namespace="payment-service")
    def handle_message(self, envelope: dict) -> None:
        print(f" [x] Mensagem recebida para o pedido: {envelope.get('order_id')}")

        # 1. Processa o pagamento (simulação com latência e tipos: PIX, cartão, boleto)
        result = process_payment(envelope)

        # 2. Publica o evento de saída (payment.approved ou payment.rejected)
        publish_event(result)

        print(f" [x] Pagamento processado para o pedido: {result['order_id']}")
        # O ACK é enviado pelo EventConsumer quando este método retorna sem erro.


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
            print(f" [*] Tentando conectar ao RabbitMQ em {RABBITMQ_HOST}...")
            connection = pika.BlockingConnection(conn_params)
            consumer = PaymentConsumer(
                connection.channel(), manager, max_retries=MAX_RETRIES
            )

            print(" [*] Aguardando mensagens. Para sair pressione CTRL+C")
            consumer.start()

        except pika.exceptions.AMQPConnectionError:
            print(
                " [!] Falha na conexão com o RabbitMQ. Tentando novamente em 5 segundos..."
            )
        except pika.exceptions.AMQPChannelError as exc:
            # Ex.: fila já existe com outros argumentos (PRECONDITION_FAILED)
            print(
                f" [!] RabbitMQ recusou a declaração/consumo: {exc}. Nova tentativa em 5 segundos..."
            )
        except KeyboardInterrupt:
            print(" [x] Consumidor interrompido pelo usuário.")
            break
        finally:
            _close_quietly(connection)

        time.sleep(RECONNECT_DELAY_SECONDS)
