import pytest
from fastapi.testclient import TestClient

from middleware.auth.token_provider import TokenManager
from services.catalog_service.main import app as catalog_app
from services.order_service.main import app as order_app


@pytest.fixture
def token_mgr():
    return TokenManager()


@pytest.fixture
def order_client():
    return TestClient(order_app)


@pytest.fixture
def catalog_client():
    return TestClient(catalog_app)


# ---------- Testes no Order Service com AuthMiddleware ----------


def test_order_service_rotas_publicas_sem_token(order_client):
    # Root
    resp = order_client.get("/")
    assert resp.status_code == 200

    # Health
    resp = order_client.get("/health")
    assert resp.status_code == 200

    # Login
    resp = order_client.post(
        "/auth/login",
        json={"username": "cliente_teste", "roles": ["customer"]},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_order_service_rota_protegida_sem_token_retorna_401(order_client):
    resp = order_client.get("/orders")
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "unauthorized"


def test_order_service_rota_protegida_com_token_expirado_retorna_401(
    order_client, token_mgr
):
    token = token_mgr.create_token(user_id="user1", expires_in_seconds=-10)
    resp = order_client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expirado" in resp.json()["detail"].lower()


def test_order_service_rota_protegida_com_token_valido_sucesso(order_client, token_mgr):
    token = token_mgr.create_token(user_id="customer_123", roles=["customer"])
    resp = order_client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------- Testes no Catalog Service com AuthMiddleware ----------


def test_catalog_service_rotas_publicas_sem_token(catalog_client):
    # Consulta ao catálogo é pública
    resp = catalog_client.get("/products")
    assert resp.status_code == 200

    # Recomendações é pública
    resp = catalog_client.get("/recommendations")
    assert resp.status_code == 200

    # Detalhes de produto existente é pública
    resp = catalog_client.get("/products/1")
    assert resp.status_code == 200


def test_catalog_service_criacao_produto_sem_token_retorna_401(catalog_client):
    payload = {
        "name": "Headset Gamer",
        "price": 350.0,
        "stock_quantity": 20,
    }
    resp = catalog_client.post("/products", json=payload)
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_catalog_service_reserva_sem_token_retorna_401(catalog_client):
    resp = catalog_client.post("/products/1/reserve", json={"quantity": 1})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_catalog_service_reserva_com_token_valido_sucesso(catalog_client, token_mgr):
    token = token_mgr.create_token(user_id="internal_order_service", roles=["internal_service"])
    resp = catalog_client.post(
        "/products/1/reserve",
        json={"quantity": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "reserved"


def test_catalog_service_criacao_produto_com_token_admin_sucesso(
    catalog_client, token_mgr
):
    token = token_mgr.create_token(user_id="admin_user", roles=["admin"])
    payload = {
        "name": "Webcam 4K Ultra",
        "price": 890.0,
        "stock_quantity": 12,
    }
    resp = catalog_client.post(
        "/products",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "Webcam 4K Ultra"
