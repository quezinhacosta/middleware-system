import logging
import sys
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from middleware.config import Settings, get_settings
from middleware.observability.health import router as health_router

logger = logging.getLogger("catalog-service")

try:
    settings: Settings = get_settings()
except ValidationError as exc:
    logger.critical(
        f"[FATAL] Erro ao carregar configuracoes obrigatorias do catalog-service: {exc}"
    )
    sys.exit(1)

app = FastAPI(
    title="Catalog Service",
    description="Microsserviço de catálogo e estoque do e-commerce",
    version="0.1.0",
)

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
