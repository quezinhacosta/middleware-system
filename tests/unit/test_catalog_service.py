from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from services.catalog_service.main import app, repository
from services.catalog_service.models import ProductCreate
from services.catalog_service.repository import (
    InsufficientStockError,
    ProductRepository,
)


@pytest.fixture(autouse=True)
def reset_repo():
    """Restaura o repositório para estado limpo antes de cada teste."""
    repository._storage.clear()
    seeds = {
        "1": {
            "id": "1",
            "name": "Notebook Pro 16",
            "price": 7500.0,
            "stock_quantity": 10,
        },
        "2": {
            "id": "2",
            "name": "Monitor Ultrawide 34",
            "price": 2800.0,
            "stock_quantity": 5,
        },
        "3": {"id": "3", "name": "Mouse Sem Fio", "price": 150.0, "stock_quantity": 0},
    }
    for k, v in seeds.items():
        repository._storage[k] = repository.create(
            ProductCreate(
                name=v["name"], price=v["price"], stock_quantity=v["stock_quantity"]
            ),
            custom_id=k,
        )
    yield


@pytest.fixture
def client():
    return TestClient(app)


def test_list_products_com_paginacao_e_filtros(client):
    response = client.get("/products?limit=2")
    assert response.status_code == 200
    assert response.headers.get("X-Total-Count") == "3"
    data = response.json()
    assert len(data) == 2
    assert data[0]["id"] == "1"

    # Filtro por nome
    res_name = client.get("/products?name=notebook")
    assert res_name.status_code == 200
    assert res_name.headers.get("X-Total-Count") == "1"
    assert len(res_name.json()) == 1
    assert res_name.json()[0]["name"] == "Notebook Pro 16"

    # Filtro por preco
    res_price = client.get("/products?min_price=2000&max_price=3000")
    assert res_price.status_code == 200
    assert res_price.headers.get("X-Total-Count") == "1"
    assert len(res_price.json()) == 1
    assert res_price.json()[0]["id"] == "2"


def test_get_product_por_id_existente(client):
    response = client.get("/products/1")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 1
    assert data["name"] == "Notebook Pro 16"
    assert data["stock_quantity"] == 10
    assert data["in_stock"] is True


def test_get_product_inexistente_retorna_404(client):
    response = client.get("/products/999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Produto não encontrado"


def test_reserve_stock_com_saldo_suficiente_retorna_200(client):
    response = client.post("/products/1/reserve", json={"quantity": 3})
    assert response.status_code == 200
    data = response.json()
    assert data["product_id"] == "1"
    assert data["reserved_quantity"] == 3
    assert data["remaining_stock"] == 7
    assert data["status"] == "reserved"

    # Consulta para confirmar decremento
    item = client.get("/products/1").json()
    assert item["stock_quantity"] == 7


def test_reserve_stock_insuficiente_retorna_409(client):
    response = client.post("/products/1/reserve", json={"quantity": 20})
    assert response.status_code == 409
    assert "Estoque insuficiente" in response.json()["detail"]


def test_reserve_stock_produto_inexistente_retorna_404(client):
    response = client.post("/products/999/reserve", json={"quantity": 1})
    assert response.status_code == 404
    assert "não encontrado" in response.json()["detail"]


def test_reserve_stock_concorrente_garante_atomicidade():
    """Valida que múltiplas threads concorrentes não causam race condition no estoque."""
    repo = ProductRepository(
        {"item_race": {"name": "Item Concorrente", "price": 10.0, "stock_quantity": 20}}
    )

    success_count = 0
    failure_count = 0

    def try_reserve():
        nonlocal success_count, failure_count
        try:
            repo.reserve_stock("item_race", 1)
            success_count += 1
        except InsufficientStockError:
            failure_count += 1

    # Dispara 50 threads concorrentes para 20 itens em estoque
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(try_reserve) for _ in range(50)]
        for f in futures:
            f.result()

    assert success_count == 20
    assert failure_count == 30
    assert repo.get_by_id("item_race").stock_quantity == 0
