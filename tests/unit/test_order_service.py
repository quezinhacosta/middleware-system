import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.order_service.main import app, order_repository
from services.order_service.models import Order, OrderItem, OrderStatus


@pytest.fixture(autouse=True)
def clean_repository():
    order_repository.clear()
    yield
    order_repository.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_post_order_cria_pedido_com_uuid_v4_e_status_pending(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    # Mock do catálogo e do broker de pagamento
    async def mock_reserve(product_id: str, quantity: int) -> dict[str, Any]:
        return {"status": "reserved", "product_id": product_id, "reserved_quantity": quantity}

    async def mock_details(product_id: int) -> dict[str, Any]:
        return {"id": product_id, "name": "Notebook Pro", "price": 5000.0, "stock_quantity": 10}

    import services.order_service.main as order_main

    monkeypatch.setattr(order_main, "reserve_catalog_stock", mock_reserve)
    monkeypatch.setattr(order_main, "fetch_product_details", mock_details)
    monkeypatch.setattr(order_main, "_enqueue_payment_for_background", lambda env: True)

    payload = {
        "items": [{"product_id": "1", "quantity": 2, "unit_price": 5000.0}],
        "payment_method": "PIX",
    }
    response = client.post(
        "/orders",
        json=payload,
        headers={"Authorization": "Bearer role=customer,sub=user_123"},
    )

    assert response.status_code == 200
    data = response.json()

    # Valida UUID v4
    order_id = data["order_id"]
    parsed_uuid = uuid.UUID(order_id, version=4)
    assert str(parsed_uuid) == order_id

    # Valida status PENDING e valor total
    assert data["status"] == "PENDING"
    assert data["customer_id"] == "user_123"
    assert data["total_amount"] == 10000.0

    # Valida persistência no repositório
    persisted = order_repository.get_by_id(order_id)
    assert persisted is not None
    assert persisted.status == OrderStatus.PENDING
    assert persisted.total_amount == 10000.0


def test_post_order_rejeita_quando_catalogo_rejeita_estoque(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from fastapi import HTTPException

    async def mock_reserve_fail(product_id: str, quantity: int) -> dict[str, Any]:
        raise HTTPException(status_code=409, detail="Estoque insuficiente no catálogo")

    import services.order_service.main as order_main

    monkeypatch.setattr(order_main, "reserve_catalog_stock", mock_reserve_fail)

    payload = {
        "product_id": "1",
        "quantity": 99,
        "payment_method": "CREDIT_CARD",
    }
    response = client.post(
        "/orders",
        json=payload,
        headers={"Authorization": "Bearer role=customer,sub=user_123"},
    )

    assert response.status_code == 409
    assert "Estoque insuficiente" in response.json()["detail"]
    # Nenhum pedido deve ser persistido em caso de rejeição
    assert len(order_repository.list_all()) == 0


def test_get_order_by_id(client: TestClient):
    order_id = str(uuid.uuid4())
    order = Order(
        id=order_id,
        customer_id="cust_abc",
        items=[OrderItem(product_id="1", quantity=1, unit_price=200.0)],
        total_amount=200.0,
        status=OrderStatus.PENDING,
        payment_method="PIX",
    )
    order_repository.save(order)

    # Consulta pelo próprio cliente
    resp_ok = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": "Bearer role=customer,sub=cust_abc"},
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json()["id"] == order_id
    assert resp_ok.json()["status"] == "PENDING"

    # Consulta por outro cliente (deve dar 403 Forbidden)
    resp_forbidden = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": "Bearer role=customer,sub=other_user"},
    )
    assert resp_forbidden.status_code == 403

    # Consulta por admin (permitido)
    resp_admin = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": "Bearer role=admin,sub=admin_user"},
    )
    assert resp_admin.status_code == 200


def test_list_orders_do_usuario_autenticado(client: TestClient):
    order_repository.save(
        Order(
            id=str(uuid.uuid4()),
            customer_id="user_A",
            items=[OrderItem(product_id="1", quantity=1, unit_price=10.0)],
            total_amount=10.0,
            status=OrderStatus.PENDING,
        )
    )
    order_repository.save(
        Order(
            id=str(uuid.uuid4()),
            customer_id="user_A",
            items=[OrderItem(product_id="2", quantity=2, unit_price=20.0)],
            total_amount=40.0,
            status=OrderStatus.CONFIRMED,
        )
    )
    order_repository.save(
        Order(
            id=str(uuid.uuid4()),
            customer_id="user_B",
            items=[OrderItem(product_id="3", quantity=1, unit_price=50.0)],
            total_amount=50.0,
            status=OrderStatus.PENDING,
        )
    )

    # user_A vê apenas seus 2 pedidos
    resp_a = client.get(
        "/orders",
        headers={"Authorization": "Bearer role=customer,sub=user_A"},
    )
    assert resp_a.status_code == 200
    assert len(resp_a.json()) == 2
    assert all(o["customer_id"] == "user_A" for o in resp_a.json())

    # admin vê todos os 3 pedidos
    resp_admin = client.get(
        "/orders",
        headers={"Authorization": "Bearer role=admin,sub=boss"},
    )
    assert resp_admin.status_code == 200
    assert len(resp_admin.json()) == 3
