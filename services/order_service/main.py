import os

from fastapi import FastAPI

from middleware.observability.logging import configure_logging

configure_logging(
    service=os.environ.get("SERVICE_NAME", "order-service"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)

app = FastAPI(
    title="Order Service",
    description="Microsserviço de gestão e checkout de pedidos do e-commerce",
    version="0.1.0",
)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "order-service", "status": "running"}


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "healthy", "service": "order-service"}
