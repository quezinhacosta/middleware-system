"""Consumidor base de eventos (RabbitMQ) com ACK/NACK, retentativas e DLQ.

Fluxo de `EventConsumer.on_message`:
  1. Corpo que não é JSON válido, ou que não é um objeto JSON -> basic_nack(requeue=False)
     direto (mensagem "envenenada": retentar não adianta) e vai para a DLQ.
  2. `handle_message(envelope)` com sucesso -> basic_ack.
  3. Erro transiente (rede, timeout, circuito aberto) -> até `max_retries` retentativas com
     backoff exponencial; esgotadas, basic_nack(requeue=False) -> DLQ.
  4. Qualquer outro erro -> basic_nack(requeue=False) imediato -> DLQ.

Topologia (declarada por `declare_topology`, no setup):
  <exchange> --routing_key--> <queue>  (x-dead-letter-exchange = <exchange>.dlx)
  <exchange>.dlx --<queue>.dlq--> <queue>.dlq   (fila de descarte/auditoria)

Observações:
  * Entrega "pelo menos uma vez": as retentativas reexecutam o handler, então
    `handle_message` deve ser idempotente.
  * O corpo da mensagem nunca é logado (pode conter dados do cliente).
  * O módulo não importa o pika: recebe o `channel` já criado (facilita testes).

Uso:
    class PaymentRequestedConsumer(EventConsumer):
        def handle_message(self, envelope: dict) -> None:
            ...

    connection = pika.BlockingConnection(pika.URLParameters(amqp_url))
    PaymentRequestedConsumer(
        connection.channel(),
        Topology(exchange="orders", queue="payments.requested", routing_key="order.created"),
    ).start()
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from middleware.resilience.circuit_breaker import CircuitBreakerOpenException
from middleware.resilience.exceptions import MiddlewareTimeoutError

logger = logging.getLogger("middleware.transport.consumer")


class UnrecoverableError(Exception):
    """Erro que retentar não resolve (dado inválido): a mensagem vai direto para a DLQ."""


class TransientError(Exception):
    """Erro temporário: a mensagem pode ser reprocessada antes de ir para a DLQ."""


def _log(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, default=str))


@dataclass(frozen=True)
class Topology:
    exchange: str
    queue: str
    routing_key: str
    exchange_type: str = "direct"

    @property
    def dlx(self) -> str:
        return f"{self.exchange}.dlx"

    @property
    def dlq(self) -> str:
        return f"{self.queue}.dlq"

    @property
    def dlq_routing_key(self) -> str:
        return self.dlq


def declare_topology(channel: Any, topology: Topology) -> None:
    """Declara DLX/DLQ primeiro e depois a fila principal apontando para a DLX.

    Atenção: se a fila principal já existir no broker com argumentos diferentes,
    o RabbitMQ recusa (PRECONDITION_FAILED). Nesse caso, apague a fila antiga.
    """
    # Fila de descarte / auditoria
    channel.exchange_declare(exchange=topology.dlx, exchange_type="direct", durable=True)
    channel.queue_declare(queue=topology.dlq, durable=True)
    channel.queue_bind(
        queue=topology.dlq, exchange=topology.dlx, routing_key=topology.dlq_routing_key
    )

    # Exchange e fila principal, com dead-lettering para a DLX
    channel.exchange_declare(
        exchange=topology.exchange, exchange_type=topology.exchange_type, durable=True
    )
    channel.queue_declare(
        queue=topology.queue,
        durable=True,
        arguments={
            "x-dead-letter-exchange": topology.dlx,
            "x-dead-letter-routing-key": topology.dlq_routing_key,
        },
    )
    channel.queue_bind(
        queue=topology.queue, exchange=topology.exchange, routing_key=topology.routing_key
    )


class EventConsumer(ABC):
    """Base para consumidores: subclasses implementam apenas `handle_message`."""

    # Erros que justificam retentativa. Subclasses podem sobrescrever.
    transient_exceptions: tuple[type[BaseException], ...] = (
        TransientError,
        ConnectionError,
        TimeoutError,
        MiddlewareTimeoutError,
        CircuitBreakerOpenException,
    )

    def __init__(
        self,
        channel: Any,
        topology: Topology,
        *,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 5.0,
        prefetch_count: int = 10,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries deve ser >= 0")
        if backoff_base < 0 or backoff_max < 0:
            raise ValueError("backoff_base e backoff_max devem ser >= 0")
        if prefetch_count < 1:
            raise ValueError("prefetch_count deve ser >= 1")
        self._channel = channel
        self._topology = topology
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._prefetch_count = prefetch_count
        self._sleep = sleep  # injetável para testes sem espera real

    @abstractmethod
    def handle_message(self, envelope: dict) -> None:
        """Processa um evento. Lance TransientError (ou erro de rede/timeout) para
        retentar e UnrecoverableError para descartar na hora."""

    # ---------- ciclo de vida ----------

    def setup(self) -> None:
        declare_topology(self._channel, self._topology)
        self._channel.basic_qos(prefetch_count=self._prefetch_count)
        self._channel.basic_consume(
            queue=self._topology.queue,
            on_message_callback=self.on_message,
            auto_ack=False,  # o ACK é sempre explícito
        )

    def start(self) -> None:
        self.setup()
        self._channel.start_consuming()

    def stop(self) -> None:
        self._channel.stop_consuming()

    # ---------- processamento ----------

    def on_message(self, ch: Any, method: Any, properties: Any, body: bytes) -> None:
        tag = method.delivery_tag
        try:
            envelope = self._decode(body)
        except UnrecoverableError as exc:
            self._dead_letter(ch, tag, reason="invalid_message", attempts=0, error=exc)
            return

        total_attempts = self._max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                self.handle_message(envelope)
            except self.transient_exceptions as exc:
                if attempt == total_attempts:
                    self._dead_letter(
                        ch, tag, reason="retries_exhausted", attempts=attempt, error=exc
                    )
                    return
                delay = self._backoff(attempt)
                _log(
                    logging.WARNING, "consumer_retry",
                    queue=self._topology.queue, delivery_tag=tag, attempt=attempt,
                    max_attempts=total_attempts, delay_s=delay, error_type=type(exc).__name__,
                )
                self._sleep(delay)
            except Exception as exc:
                self._dead_letter(
                    ch, tag, reason="unrecoverable_error", attempts=attempt, error=exc
                )
                return
            else:
                ch.basic_ack(delivery_tag=tag)
                _log(
                    logging.DEBUG, "consumer_ack",
                    queue=self._topology.queue, delivery_tag=tag, attempts=attempt,
                )
                return

    # ---------- internos ----------

    @staticmethod
    def _decode(body: bytes) -> dict:
        try:
            envelope = json.loads(body)
        except (TypeError, ValueError) as exc:  # JSONDecodeError e UnicodeDecodeError
            raise UnrecoverableError("corpo não é JSON válido") from exc
        if not isinstance(envelope, dict):
            raise UnrecoverableError("envelope deve ser um objeto JSON")
        return envelope

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_max)

    def _dead_letter(
        self, ch: Any, tag: int, *, reason: str, attempts: int, error: BaseException
    ) -> None:
        ch.basic_nack(delivery_tag=tag, requeue=False)  # RabbitMQ roteia para a DLX/DLQ
        _log(
            logging.WARNING, "consumer_dead_letter",
            queue=self._topology.queue, dlq=self._topology.dlq, delivery_tag=tag,
            reason=reason, attempts=attempts, error_type=type(error).__name__,
            error=str(error)[:200],
        )