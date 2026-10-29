import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from middleware.observability.correlation import CorrelationIdMiddleware
from middleware.observability.logging import configure_logging
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
    title="Notification Service",
    description="Notificações de pós-venda e alertas simulados ao cliente",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "healthy", "service": "notification-service"}


@app.get("/notifications")
def list_notifications() -> list[dict[str, Any]]:
    """Últimas notificações enviadas, da mais antiga para a mais recente."""
    return list(history)
