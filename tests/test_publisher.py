import json
import uuid

from middleware.transport.publisher import EventPublisher, RabbitMQPublisher


class FakePublisher(EventPublisher):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(producer_service="order-service")
        self.fail = fail
        self.sent: list[tuple[str, bytes]] = []

    def _transmit(self, queue: str, body: bytes) -> None:
        if self.fail:
            raise ConnectionError("broker indisponível")
        self.sent.append((queue, body))


def test_publica_envelope_padrao_com_correlation_id() -> None:
    publisher = FakePublisher()

    ok = publisher.publish(
        "order.created",
        "order.created",
        {"order_id": 7, "total": 150.0},
        correlation_id="corr-123",
    )

    assert ok is True
    queue, body = publisher.sent[0]
    envelope = json.loads(body)
    assert queue == "order.created"
    assert envelope["event_name"] == "order.created"
    assert envelope["correlation_id"] == "corr-123"
    assert envelope["producer_service"] == "order-service"
    assert envelope["data"] == {"order_id": 7, "total": 150.0}
    assert str(uuid.UUID(envelope["event_id"], version=4)) == envelope["event_id"]
    assert envelope["timestamp"].endswith("Z")


def test_event_id_e_unico_por_mensagem() -> None:
    publisher = FakePublisher()

    publisher.publish("q", "e", {}, correlation_id="c")
    publisher.publish("q", "e", {}, correlation_id="c")

    first, second = (json.loads(body)["event_id"] for _, body in publisher.sent)
    assert first != second


def test_falha_de_transporte_retorna_false_sem_propagar() -> None:
    publisher = FakePublisher(fail=True)

    ok = publisher.publish("q", "e", {}, correlation_id="c")

    assert ok is False
    assert publisher.sent == []


def test_rabbitmq_publisher_com_broker_inacessivel_nao_trava() -> None:
    publisher = RabbitMQPublisher(
        "amqp://guest:guest@127.0.0.1:1/",
        producer_service="order-service",
        timeout=0.5,
    )

    ok = publisher.publish("order.created", "order.created", {}, correlation_id="c")

    assert ok is False
