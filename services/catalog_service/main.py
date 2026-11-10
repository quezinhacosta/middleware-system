import logging
import os
import sys
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from pydantic import ValidationError

from middleware.auth.rbac import UserIdentity, require_roles
from middleware.config import Settings, get_settings
from middleware.observability.correlation import CorrelationIdMiddleware
from middleware.observability.health import router as health_router
from middleware.observability.logging import configure_logging
from services.catalog_service.models import (
    Product,
    ProductCreate,
    ReserveStockRequest,
    ReserveStockResponse,
)
from services.catalog_service.repository import (
    InsufficientStockError,
    ProductNotFoundError,
    ProductRepository,
)

logger = logging.getLogger("catalog-service")

try:
    settings: Settings = get_settings()
except ValidationError as exc:
    logger.critical(
        f"[FATAL] Erro ao carregar configuracoes obrigatorias do catalog-service: {exc}"
    )
    sys.exit(1)

configure_logging(
    service=os.environ.get("SERVICE_NAME", "catalog-service"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)

app = FastAPI(
    title="Catalog Service",
    description="Microsserviço de catálogo e estoque do e-commerce",
    version="0.1.0",
)
app.add_middleware(CorrelationIdMiddleware)

app.include_router(health_router)

# Dados mock (seeds) iniciais para popular o repositório
INITIAL_SEEDS: dict[str, dict[str, Any]] = {
    "1": {
        "id": "1",
        "name": "Notebook Pro 16",
        "price": 7500.00,
        "stock_quantity": 15,
        "details": "16GB RAM, 512GB NVMe SSD",
    },
    "2": {
        "id": "2",
        "name": "Monitor Ultrawide 34",
        "price": 2800.00,
        "stock_quantity": 8,
        "details": "IPS, 144Hz, HDR",
    },
    "3": {
        "id": "3",
        "name": "Teclado Mecânico Sem Fio",
        "price": 650.00,
        "stock_quantity": 25,
        "details": "Switch Tactile, Bluetooth 5.1",
    },
}

# Instância singleton do repositório em memória
repository = ProductRepository(INITIAL_SEEDS)


@app.get("/")
def read_root() -> dict[str, str]:
    return {
        "service": settings.SERVICE_NAME,
        "status": "running",
        "environment": settings.ENVIRONMENT,
    }


@app.get("/recommendations")
def get_recommendations() -> dict[str, Any]:
    products = repository.list_products(limit=10)
    items = [
        {
            "id": p.id,
            "name": p.name,
            "price": p.price,
            "in_stock": p.stock_quantity > 0,
            "stock_quantity": p.stock_quantity,
        }
        for p in products
    ]
    return {
        "items": items,
        "degraded": False,
    }


@app.get("/products", response_model=list[Product])
def list_products(
    response: Response,
    skip: int = Query(0, ge=0, description="Itens para pular (paginação)"),
    limit: int = Query(20, ge=1, le=100, description="Limite por página"),
    name: str | None = Query(None, description="Filtro por nome do produto"),
    min_price: float | None = Query(None, ge=0.0, description="Preço mínimo"),
    max_price: float | None = Query(None, ge=0.0, description="Preço máximo"),
) -> list[Product]:
    """Listagem de produtos com suporte a paginação, filtros e cabeçalho X-Total-Count."""
    total = repository.count_products(
        name=name, min_price=min_price, max_price=max_price
    )
    response.headers["X-Total-Count"] = str(total)
    return repository.list_products(
        skip=skip,
        limit=limit,
        name=name,
        min_price=min_price,
        max_price=max_price,
    )


@app.get("/products/{product_id}")
def get_product(product_id: str) -> dict[str, Any]:
    """Detalhes de um produto específico."""
    product = repository.get_by_id(product_id)
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Produto não encontrado",
        )
    return {
        "id": int(product.id) if product.id.isdigit() else product.id,
        "name": product.name,
        "price": product.price,
        "stock_quantity": product.stock_quantity,
        "in_stock": product.stock_quantity > 0,
        "degraded": False,
    }


@app.post(
    "/products/{product_id}/reserve",
    response_model=ReserveStockResponse,
    status_code=status.HTTP_200_OK,
)
def reserve_product_stock(
    product_id: str,
    payload: ReserveStockRequest | None = None,
) -> ReserveStockResponse:
    """Reserva temporária de quantidade com validação atômica de concorrência."""
    quantity = payload.quantity if payload is not None else 1
    try:
        reserved_qty, remaining = repository.reserve_stock(product_id, quantity)
        return ReserveStockResponse(
            product_id=str(product_id),
            reserved_quantity=reserved_qty,
            remaining_stock=remaining,
            status="reserved",
        )
    except ProductNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InsufficientStockError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@app.post("/products", status_code=status.HTTP_201_CREATED)
def create_product(
    product: ProductCreate,
    user: UserIdentity = Depends(require_roles(["admin"])),
) -> dict[str, Any]:
    """Criação de novos produtos protegida por RBAC (papel admin)."""
    created = repository.create(product)
    return {
        "message": f"Produto cadastrado por {user.user_id}",
        "product_id": int(created.id) if created.id.isdigit() else created.id,
    }
