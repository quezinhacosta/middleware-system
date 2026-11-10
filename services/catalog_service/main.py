import logging
import os
import sys
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from middleware.auth.rbac import UserIdentity, require_roles
from middleware.config import Settings, get_settings
from middleware.observability.correlation import CorrelationIdMiddleware
from middleware.observability.health import router as health_router
from middleware.observability.logging import configure_logging

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

CATALOG_PRODUCTS: dict[int, dict[str, Any]] = {
    1: {
        "id": 1,
        "name": "Notebook Pro 16",
        "price": 7500.00,
        "in_stock": True,
        "details": "16GB RAM, 512GB NVMe SSD",
    },
    2: {
        "id": 2,
        "name": "Monitor Ultrawide 34",
        "price": 2800.00,
        "in_stock": True,
        "details": "IPS, 144Hz, HDR",
    },
    3: {
        "id": 3,
        "name": "Teclado Mecânico Sem Fio",
        "price": 650.00,
        "in_stock": True,
        "details": "Switch Tactile, Bluetooth 5.1",
    },
}


@app.get("/")
def read_root() -> dict[str, str]:
    return {
        "service": settings.SERVICE_NAME,
        "status": "running",
        "environment": settings.ENVIRONMENT,
    }


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": settings.SERVICE_NAME,
    }


@app.get("/recommendations")
def get_recommendations() -> dict[str, Any]:
    return {
        "items": list(CATALOG_PRODUCTS.values()),
        "degraded": False,
    }


@app.get("/products/{product_id}")
def get_product(product_id: int) -> dict[str, Any]:
    product = CATALOG_PRODUCTS.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    return {**product, "degraded": False}


class ProductCreate(BaseModel):
    name: str
    price: float
    details: str


@app.post("/products", status_code=201)
def create_product(
    product: ProductCreate,
    user: UserIdentity = Depends(require_roles(["admin"])),
) -> dict[str, Any]:
    # Lógica simplificada de inserção
    new_id = max(CATALOG_PRODUCTS.keys()) + 1 if CATALOG_PRODUCTS else 1
    CATALOG_PRODUCTS[new_id] = {
        "id": new_id,
        "name": product.name,
        "price": product.price,
        "in_stock": True,
        "details": product.details,
    }
    return {"message": f"Produto cadastrado por {user.user_id}", "product_id": new_id}
