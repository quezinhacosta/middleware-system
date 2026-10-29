import os

from fastapi import FastAPI

from middleware.observability.logging import configure_logging

configure_logging(
    service=os.environ.get("SERVICE_NAME", "catalog-service"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)

app = FastAPI(
    title="Catalog Service",
    description="Microsserviço de catálogo e estoque do e-commerce",
    version="0.1.0",
)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "catalog-service", "status": "running"}


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "healthy", "service": "catalog-service"}
