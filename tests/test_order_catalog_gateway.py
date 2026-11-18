"""Rotas do order-service que expõem o catálogo (privado) para o navegador."""

import httpx
import pytest
from fastapi.testclient import TestClient

from middleware.auth.token_provider import TokenManager
from services.order_service import main as order_main


class FakeCatalogClient:
    def __init__(self, response: httpx.Response):
        self.response = response
        self.calls: list[tuple[str, str, dict | None]] = []

    async def post(self, url, json=None, **kwargs):
        self.calls.append(("POST", url, json))
        return self.response

    async def get_catalog(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        return self.response


def _resp(status_code: int, body) -> httpx.Response:
    return httpx.Response(status_code, json=body, request=httpx.Request("GET", "x"))


def _auth(role: str) -> dict[str, str]:
    token = TokenManager().create_token(user_id=f"user-{role}", roles=[role])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    return TestClient(order_main.app)


@pytest.fixture
def fake(monkeypatch):
    fake = FakeCatalogClient(_resp(201, {"message": "ok", "product_id": 4}))
    monkeypatch.setattr(order_main, "get_http_client", lambda: fake)
    return fake


PRODUTO = {"name": "Cadeira Gamer", "price": 1299.9, "stock_quantity": 5}


def test_admin_cadastra_produto_pelo_order_service(client, fake):
    resp = client.post("/catalog/products", json=PRODUTO, headers=_auth("admin"))

    assert resp.status_code == 201
    assert resp.json()["product_id"] == 4
    assert fake.calls == [
        ("POST", f"{order_main.CATALOG_SERVICE_URL}/products", PRODUTO)
    ]


def test_cliente_nao_cadastra_produto(client, fake):
    resp = client.post("/catalog/products", json=PRODUTO, headers=_auth("customer"))

    assert resp.status_code == 403
    assert fake.calls == []  # barrado na borda, sem chamar o catálogo


def test_sem_token_nao_cadastra_produto(client, fake):
    resp = client.post("/catalog/products", json=PRODUTO)

    assert resp.status_code in (401, 403)
    assert fake.calls == []


def test_payload_invalido_e_rejeitado(client, fake):
    resp = client.post(
        "/catalog/products",
        json={"name": "", "price": 0},
        headers=_auth("admin"),
    )

    assert resp.status_code == 422
    assert fake.calls == []


def test_erro_do_catalogo_e_repassado(client, fake):
    fake.response = _resp(403, {"detail": "Permissão insuficiente"})

    resp = client.post("/catalog/products", json=PRODUTO, headers=_auth("admin"))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Permissão insuficiente"


def test_lista_produtos_do_catalogo(client, fake):
    fake.response = _resp(200, [{"id": "1", "name": "Notebook"}])

    resp = client.get("/catalog/products")

    assert resp.status_code == 200
    assert resp.json() == [{"id": "1", "name": "Notebook"}]