import os
import threading
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from src.consumer import (
    IDEMPOTENCY_NAMESPACE,
    get_idempotency_manager,
    process_and_publish,
    start_consuming,
)

from middleware.observability.correlation import (
    CorrelationIdMiddleware,
    get_correlation_id,
)
from middleware.observability.health import router as health_router
from middleware.observability.logging import configure_logging
from middleware.observability.metrics import (
    MetricsCollector,
    MetricsMiddleware,
    build_metrics_router,
)
from middleware.observability.openapi_responses import COMMON_RESPONSES
from middleware.transport.idempotency import MessageInProgressError

metrics = MetricsCollector(service=os.getenv("SERVICE_NAME", "payment-service"))

configure_logging(
    service=os.environ.get("SERVICE_NAME", "payment-service"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Sobe o consumer do RabbitMQ em uma thread em background
    enquanto o FastAPI atende os endpoints HTTP.
    """
    consumer_thread = threading.Thread(
        target=start_consuming,
        name="rabbitmq-consumer",
        daemon=True,  # encerra junto com o processo principal
    )
    consumer_thread.start()
    yield


app = FastAPI(
    title="Payment Service API",
    description="Microsserviço de processamento assíncrono e liquidação de pagamentos.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(MetricsMiddleware, collector=metrics)
app.include_router(health_router)
app.include_router(build_metrics_router(metrics))


@app.get("/health", tags=["Healthcheck"])
def healthcheck() -> dict[str, str]:
    return {
        "status": "UP",
        "service": os.environ.get("SERVICE_NAME", "payment-service"),
    }


class PaymentRequest(BaseModel):
    """Pedido de autorização enviado pelo order-service."""

    model_config = ConfigDict(extra="allow")

    event_id: str = Field(
        min_length=1, description="Chave de idempotência do pagamento"
    )
    order_id: str = Field(min_length=1, description="Pedido a ser pago")
    amount: float | None = Field(default=None, ge=0, description="Valor total")
    payment_method: str = Field(default="PIX", description="Forma de pagamento")
    correlation_id: str | None = Field(default=None, description="ID de rastreamento")


@app.post(
    "/payments",
    tags=["Pagamentos"],
    responses={409: COMMON_RESPONSES[409], 503: COMMON_RESPONSES[503]},
)
def authorize_payment(req: PaymentRequest) -> dict[str, Any]:
    """Processa o pagamento de forma síncrona e publica o resultado no broker.

    Usa o mesmo registro de idempotência do consumidor da fila ``order.created``:
    se o order-service reenviar o pedido pela fila (fallback), ele não é cobrado
    de novo.
    """
    order_data = req.model_dump()
    order_data["correlation_id"] = req.correlation_id or get_correlation_id()
    result: dict[str, Any] = {}

    def _process() -> None:
        result.update(process_and_publish(order_data))

    try:
        executed = get_idempotency_manager().run_once(
            f"{IDEMPOTENCY_NAMESPACE}:{req.event_id}", _process
        )
    except MessageInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pagamento deste evento já está em processamento",
        ) from exc
    except Exception as exc:
        # Broker fora ou falha no processamento: o order-service aciona o fallback
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Não foi possível concluir o pagamento agora",
        ) from exc

    if not executed:
        return {
            "order_id": req.order_id,
            "event_id": req.event_id,
            "status": "payment.duplicate",
            "message": "Pagamento deste evento já foi processado anteriormente",
        }
    result.pop("correlation_id", None)
    return {**result, "event_id": req.event_id}


if __name__ == "__main__":
    print("=== Iniciando Payment Service (Worker + Health API) ===")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8002")))
