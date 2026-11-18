import os
import threading
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from src.consumer import start_consuming

from middleware.observability.health import router as health_router
from middleware.observability.metrics import (
    MetricsCollector,
    MetricsMiddleware,
    build_metrics_router,
)

metrics = MetricsCollector(service=os.getenv("SERVICE_NAME", "payment-service"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Sobe o consumer do RabbitMQ em uma thread em background
    enquanto o FastAPI atende os endpoints /health/*.
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
app.add_middleware(MetricsMiddleware, collector=metrics)
app.include_router(health_router)
app.include_router(build_metrics_router(metrics))


if __name__ == "__main__":
    print("=== Iniciando Payment Service (Worker + Health API) ===")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8002")))
