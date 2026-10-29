import os

from fastapi import FastAPI

from middleware.observability.correlation import CorrelationIdMiddleware
from middleware.observability.logging import configure_logging
from middleware.observability.metrics import MetricsCollector, MetricsMiddleware

SERVICE_NAME = os.environ.get("SERVICE_NAME", "order-service")

configure_logging(
    service=SERVICE_NAME,
    level=os.environ.get("LOG_LEVEL", "INFO"),
)

metrics = MetricsCollector(service=SERVICE_NAME)

app = FastAPI(
    title="Order Service",
    description="Microsserviço de gestão e checkout de pedidos do e-commerce",
    version="0.1.0",
)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(MetricsMiddleware, collector=metrics)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "order-service", "status": "running"}


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "healthy", "service": "order-service"}
