from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import pika
import pika.exceptions

from middleware.observability.correlation import correlation_id_var
from middleware.transport.consumer import (
    EventConsumer,
    Topology,
    UnrecoverableError,
    declare_topology,
)

logger = logging.getLogger("order-service.payment-events")

PAYMENTS_EXCHANGE = "payments"
PAYMENT_APPROVED = "payment.approved"
PAYMENT_REJECTED = "payment.rejected"
PAYMENT_RESULTS = (PAYMENT_APPROVED, PAYMENT_REJECTED)
RESULTS_QUEUE = "order-service.payment-results"
RECONNECT_DELAY_SECONDS = 5.0

TOPOLOGY = Topology(
    exchange=PAYMENTS_EXCHANGE, queue=RESULTS_QUEUE, routing_key=PAYMENT_APPROVED
)

# Recebe (order_id, status do pagamento)
ResultHandler = Callable[[str, str], None]


def parse_payment_result(envelope: dict[str, Any]) -> tuple[str, str]:
    """Extrai (order_id, status) do envelope padrão ou do formato plano."""
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = envelope
    order_id = data.get("order_id")
    payment_status = data.get("status") or envelope.get("event_name")
    if not isinstance(order_id, str) or not order_id:
        raise UnrecoverableError("evento de pagamento sem order_id")
    if payment_status not in PAYMENT_RESULTS:
        raise UnrecoverableError(f"status de pagamento desconhecido: {payment_status}")
    return order_id, payment_status


class PaymentResultConsumer(EventConsumer):
    """Fila própria do order-service, ligada às duas routing keys de resultado."""

    def __init__(self, channel: Any, on_result: ResultHandler, **kwargs: Any) -> None:
        super().__init__(channel, TOPOLOGY, **kwargs)
        self._on_result = on_result

    def setup(self) -> None:
        declare_topology(self._channel, TOPOLOGY)
        # declare_topology liga uma routing key; a segunda é ligada aqui
        self._channel.queue_bind(
            queue=RESULTS_QUEUE,
            exchange=PAYMENTS_EXCHANGE,
            routing_key=PAYMENT_REJECTED,
        )
        self._channel.basic_qos(prefetch_count=self._prefetch_count)
        self._channel.basic_consume(
            queue=RESULTS_QUEUE, on_message_callback=self.on_message, auto_ack=False
        )

    def handle_message(self, envelope: dict) -> None:
        order_id, payment_status = parse_payment_result(envelope)
        token = correlation_id_var.set(envelope.get("correlation_id"))
        try:
            self._on_result(order_id, payment_status)
        finally:
            correlation_id_var.reset(token)


class PaymentResultListener:
    """Mantém o consumidor rodando em uma thread, reconectando se o broker cair."""

    def __init__(self, params: pika.ConnectionParameters, on_result: ResultHandler):
        self._params = params
        self._on_result = on_result
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="payment-results-consumer", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            connection = None
            try:
                connection = pika.BlockingConnection(self._params)
                consumer = PaymentResultConsumer(connection.channel(), self._on_result)
                logger.info("consumindo resultados de pagamento em %s", RESULTS_QUEUE)
                consumer.start()
            except (pika.exceptions.AMQPError, OSError):
                logger.warning(
                    "broker indisponível para resultados de pagamento, nova tentativa em %.0fs",
                    RECONNECT_DELAY_SECONDS,
                )
            finally:
                try:
                    if connection is not None and connection.is_open:
                        connection.close()
                except Exception:
                    pass
            self._stop.wait(RECONNECT_DELAY_SECONDS)
