import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from middleware.observability.correlation import CorrelationIdMiddleware
from middleware.observability.health import router as health_router
from middleware.observability.logging import configure_logging
from middleware.observability.openapi_responses import COMMON_RESPONSES
from services.notification_service.consumer import NotificationConsumer
from services.notification_service.notifications import handle_event, history

# Filas de status que disparam notificações ao cliente
QUEUES = ["payment.approved", "payment.rejected", "order.cancelled"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    amqp_url = os.environ.get("AMQP_URL")
    consumer = None
    if amqp_url:
        consumer = NotificationConsumer(amqp_url, QUEUES, handle_event)
        consumer.start()
    yield
    if consumer is not None:
        consumer.stop()


configure_logging(
    service=os.environ.get("SERVICE_NAME", "notification-service"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)

app = FastAPI(
    title="Notification Service API",
    description="Microsserviço de notificações de pós-venda e alertas ao cliente.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)
app.include_router(health_router)


@app.get("/health", tags=["Observabilidade"])
def healthcheck() -> dict[str, str]:
    return {"status": "healthy", "service": "notification-service"}


@app.get(
    "/notifications",
    tags=["Notificações"],
    responses={500: COMMON_RESPONSES[500]},
)
def list_notifications() -> list[dict[str, Any]]:
    """Últimas notificações enviadas, da mais antiga para a mais recente."""
    return list(history)
