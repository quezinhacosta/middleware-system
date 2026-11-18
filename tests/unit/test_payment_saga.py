"""Fluxo de resultado do pagamento: envelope publicado pelo payment-service e
transição de status do pedido no order-service (SAGA)."""

import importlib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from middleware.transport.consumer import UnrecoverableError
from middleware.transport.publisher import EventEnvelope
from services.order_service import main as order_main
from services.order_service.models import Order, OrderItem, OrderStatus
from services.order_service.payment_events import parse_payment_result

PAYMENT_DIR = Path(__file__).resolve().parents[2] / "services" / "payment_service"


@pytest.fixture
def payment_src(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    monkeypatch.syspath_prepend(str(PAYMENT_DIR))  # o serviço importa `src.*`
    return (
        importlib.import_module("src.publisher"),
        importlib.import_module("src.consumer"),
    )


# ---------- payment-service ----------


def test_resultado_e_publicado_no_envelope_que_o_notification_entende(
    payment_src: tuple[ModuleType, ModuleType],
) -> None:
    publisher, _ = payment_src
    envelope = publisher.build_envelope(
        {
            "order_id": "ord-1",
            "payment_id": "pay_1",
            "status": "payment.approved",
            "correlation_id": "corr-1",
        }
    )

    # mesmo parser usado pelo notification-service
    parsed = EventEnvelope.model_validate_json(envelope.model_dump_json())
    assert parsed.event_name == "payment.approved"
    assert parsed.correlation_id == "corr-1"
    assert parsed.producer_service == "payment-service"
    assert parsed.data == {
        "order_id": "ord-1",
        "payment_id": "pay_1",
        "status": "payment.approved",
    }


def test_envelope_gera_correlation_id_quando_ausente(
    payment_src: tuple[ModuleType, ModuleType],
) -> None:
    publisher, _ = payment_src
    envelope = publisher.build_envelope({"order_id": "o", "status": "payment.rejected"})
    assert envelope.correlation_id


def test_consumer_aceita_envelope_padrao_e_formato_plano(
    payment_src: tuple[ModuleType, ModuleType],
) -> None:
    _, consumer = payment_src
    plano = {"event_id": "e1", "order_id": "o1"}
    assert consumer.extract_order_data(plano) is plano

    envelope = {"event_id": "e2", "correlation_id": "c2", "data": {"order_id": "o2"}}
    assert consumer.extract_order_data(envelope) == {
        "order_id": "o2",
        "correlation_id": "c2",
    }


# ---------- order-service ----------


@pytest.mark.parametrize(
    "envelope",
    [
        {"event_name": "payment.approved", "data": {"order_id": "o1"}},
        {"order_id": "o1", "status": "payment.approved"},
    ],
)
def test_parse_payment_result(envelope: dict[str, Any]) -> None:
    assert parse_payment_result(envelope) == ("o1", "payment.approved")


@pytest.mark.parametrize(
    "envelope",
    [
        {"data": {"status": "payment.approved"}},
        {"data": {"order_id": "o1", "status": "payment.unknown"}},
    ],
)
def test_parse_payment_result_invalido_vai_para_dlq(envelope: dict[str, Any]) -> None:
    with pytest.raises(UnrecoverableError):
        parse_payment_result(envelope)


@pytest.fixture
def pedido_pendente() -> Iterator[Order]:
    order_main.order_repository.clear()
    order_main.ORDER_RESERVATIONS.clear()
    order = Order(
        id="ord-1",
        customer_id="ana",
        items=[OrderItem(product_id="1", quantity=2, unit_price=10.0)],
        total_amount=20.0,
    )
    order_main.order_repository.save(order)
    order_main.ORDER_RESERVATIONS["ord-1"] = [("1", 2)]
    yield order
    order_main.order_repository.clear()
    order_main.ORDER_RESERVATIONS.clear()


def test_pagamento_aprovado_confirma_pedido_sem_liberar_estoque(
    pedido_pendente: Order, monkeypatch: pytest.MonkeyPatch
) -> None:
    liberados: list[tuple[str, int]] = []
    monkeypatch.setattr(
        order_main, "_release_stock_sync", lambda p, q: liberados.append((p, q))
    )

    order_main.handle_payment_result_event("ord-1", "payment.approved")

    pedido = order_main.order_repository.get_by_id("ord-1")
    assert pedido is not None
    assert pedido.status == OrderStatus.CONFIRMED
    assert liberados == []
    assert "ord-1" not in order_main.ORDER_RESERVATIONS


def test_pagamento_recusado_cancela_e_compensa_estoque_uma_unica_vez(
    pedido_pendente: Order, monkeypatch: pytest.MonkeyPatch
) -> None:
    liberados: list[tuple[str, int]] = []
    monkeypatch.setattr(
        order_main, "_release_stock_sync", lambda p, q: liberados.append((p, q))
    )

    order_main.handle_payment_result_event("ord-1", "payment.rejected")
    order_main.handle_payment_result_event("ord-1", "payment.rejected")  # duplicado

    pedido = order_main.order_repository.get_by_id("ord-1")
    assert pedido is not None
    assert pedido.status == OrderStatus.CANCELLED
    assert liberados == [("1", 2)]


def test_resultado_nao_reverte_pedido_ja_finalizado(
    pedido_pendente: Order, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(order_main, "_release_stock_sync", lambda p, q: None)
    order_main.handle_payment_result_event("ord-1", "payment.approved")
    order_main.handle_payment_result_event("ord-1", "payment.rejected")

    pedido = order_main.order_repository.get_by_id("ord-1")
    assert pedido is not None
    assert pedido.status == OrderStatus.CONFIRMED


def test_post_order_aplica_resultado_do_pagamento_sincrono(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from middleware.auth.token_provider import TokenManager

    order_main.order_repository.clear()

    async def fake_reserve(product_id: str, quantity: int) -> dict[str, float]:
        return {"unit_price": 50.0}

    async def fake_payment(payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["correlation_id"] == "corr-xyz"
        return {
            "order_id": payload["order_id"],
            "status": "payment.approved",
            "degraded": False,
        }

    monkeypatch.setattr(order_main, "reserve_catalog_stock", fake_reserve)
    monkeypatch.setattr(order_main, "request_payment_processing", fake_payment)

    token = TokenManager().create_token(user_id="ana", roles=["customer"])
    resp = TestClient(order_main.app).post(
        "/orders",
        json={"product_id": "1", "quantity": 1, "payment_method": "PIX"},
        headers={"Authorization": f"Bearer {token}", "X-Correlation-ID": "corr-xyz"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["degraded"] is False
    assert body["status"] == "CONFIRMED"
    order_main.order_repository.clear()
