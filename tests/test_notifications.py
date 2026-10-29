import json
from collections.abc import Iterator

import pydantic
import pytest
from fastapi.testclient import TestClient

from middleware.transport.publisher import EventEnvelope
from services.notification_service.main import app
from services.notification_service.notifications import (
    HISTORY_SIZE,
    build_notification,
    handle_event,
    history,
)


def make_body(event_name: str, data: dict[str, object]) -> bytes:
    envelope = EventEnvelope(
        event_name=event_name,
        correlation_id="corr-77",
        producer_service="payment-service",
        data=data,
    )
    return envelope.model_dump_json().encode()


@pytest.fixture(autouse=True)
def clear_history() -> Iterator[None]:
    history.clear()
    yield
    history.clear()


def test_monta_notificacao_para_pagamento_aprovado() -> None:
    envelope = EventEnvelope.model_validate_json(
        make_body("payment.approved", {"order_id": 9})
    )

    notification = build_notification(envelope)

    assert notification["message"] == "Pagamento do pedido 9 aprovado"
    assert notification["correlation_id"] == "corr-77"
    assert notification["to"] == "cliente-desconhecido"


def test_usa_email_do_cliente_quando_informado() -> None:
    body = make_body("order.cancelled", {"order_id": 3, "customer_email": "a@b.com"})

    notification = handle_event(body)

    assert notification["to"] == "a@b.com"
    assert notification["message"] == "Pedido 3 cancelado"


def test_evento_desconhecido_levanta_erro() -> None:
    envelope = EventEnvelope.model_validate_json(make_body("order.created", {}))

    with pytest.raises(KeyError):
        build_notification(envelope)


def test_mensagem_malformada_levanta_erro() -> None:
    with pytest.raises(pydantic.ValidationError):
        handle_event(json.dumps({"sem": "envelope"}).encode())


def test_historia_guarda_somente_as_ultimas_100() -> None:
    for order_id in range(HISTORY_SIZE + 5):
        handle_event(make_body("payment.rejected", {"order_id": order_id}))

    assert len(history) == HISTORY_SIZE
    assert history[-1]["message"] == f"Pagamento do pedido {HISTORY_SIZE + 4} recusado"


def test_endpoint_lista_notificacoes_enviadas() -> None:
    handle_event(make_body("payment.approved", {"order_id": 1}))

    response = TestClient(app).get("/notifications")

    assert response.status_code == 200
    assert response.json()[0]["message"] == "Pagamento do pedido 1 aprovado"


def test_health_do_notification_service() -> None:
    response = TestClient(app).get("/health")

    assert response.json() == {"status": "healthy", "service": "notification-service"}
