import logging
import sys

from fastapi import FastAPI
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
