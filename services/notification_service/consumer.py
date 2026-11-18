"""Consumidor AMQP que escuta as filas de status em uma thread de fundo."""

import logging
import threading
from collections.abc import Callable

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import AMQPError
from pika.spec import Basic, BasicProperties

from middleware.observability.metrics import MetricsCollector

logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 2.0
PREFETCH_COUNT = 10


class NotificationConsumer:
    """Mantém a conexão com o broker e entrega cada mensagem ao handler.

    Se o broker cair, a conexão é refeita após ``RETRY_DELAY_SECONDS``.
    Mensagens que falham no handler são rejeitadas sem reenfileirar.
    """

    def __init__(
        self,
        amqp_url: str,
        queues: list[str],
        handler: Callable[[bytes], object],
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._params = pika.URLParameters(amqp_url)
        self._queues = queues
        self._handler = handler
        self._metrics = metrics
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                connection = pika.BlockingConnection(self._params)
                channel = connection.channel()
                channel.basic_qos(prefetch_count=PREFETCH_COUNT)
                for queue in self._queues:
                    channel.queue_declare(queue=queue, durable=True)
                    channel.basic_consume(
                        queue=queue, on_message_callback=self._on_message
                    )
                logger.info("consumindo filas %s", ", ".join(self._queues))
                channel.start_consuming()
            except (AMQPError, OSError):
                logger.warning(
                    "broker indisponível, nova tentativa em %.0fs",
                    RETRY_DELAY_SECONDS,
                )
                self._stop.wait(RETRY_DELAY_SECONDS)

    def _on_message(
        self,
        channel: BlockingChannel,
        method: Basic.Deliver,
        properties: BasicProperties,
        body: bytes,
    ) -> None:
        queue = method.routing_key
        try:
            self._handler(body)
        except Exception:
            logger.exception("falha ao processar mensagem da fila")
            self._record(queue, "error")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        self._record(queue, "ok")
        channel.basic_ack(delivery_tag=method.delivery_tag)

    def _record(self, queue: str, status: str) -> None:
        if self._metrics is not None:
            self._metrics.observe_message(queue, status)
