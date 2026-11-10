import json
import logging
import os
import sys
import uuid
from typing import Any

import httpx
import pika
from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field, ValidationError

from middleware.auth.rbac import UserIdentity, require_roles
from middleware.config import Settings, get_settings
from middleware.http.distributed_http_client import DistributedHttpClient
from middleware.observability.correlation import CorrelationIdMiddleware
from middleware.observability.health import router as health_router
from middleware.observability.logging import configure_logging
from middleware.resilience.circuit_breaker import CircuitBreaker
from middleware.resilience.exceptions import MiddlewareTimeoutError
from middleware.resilience.fallback import with_fallback
from middleware.resilience.handlers import register_timeout_handler
from middleware.resilience.retry import RetryPolicy
from middleware.transport.consumer import Topology, declare_topology

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("order-service")

try:
    settings: Settings = get_settings()
except ValidationError as exc:
    logger.critical(
        f"[FATAL] Erro ao carregar configuracoes obrigatorias do order-service: {exc}"
    )
    sys.exit(1)

CATALOG_SERVICE_URL = os.getenv("CATALOG_SERVICE_URL", "http://catalog-service:8001")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8002")
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "message-broker")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")
PAYMENT_QUEUE = os.getenv("PAYMENT_QUEUE", "order.created")

# Cache local de contingência para produtos quando o Catalog Service estiver indisponível
LOCAL_PRODUCT_CACHE: dict[int, dict[str, Any]] = {
    1: {
        "id": 1,
        "name": "Notebook Pro 16 (Cache Local)",
        "price": 7500.00,
        "in_stock": True,
    },
    2: {
        "id": 2,
        "name": "Monitor Ultrawide 34 (Cache Local)",
        "price": 2800.00,
        "in_stock": True,
    },
}

catalog_breaker = CircuitBreaker(
    name="catalog-service",
    failure_threshold=3,
    recovery_timeout=10.0,
    expected_exceptions=(
        MiddlewareTimeoutError,
        httpx.HTTPError,
        ConnectionError,
        TimeoutError,
        OSError,
    ),
)

payment_breaker = CircuitBreaker(
    name="payment-service",
    failure_threshold=3,
    recovery_timeout=10.0,
    expected_exceptions=(
        MiddlewareTimeoutError,
        httpx.HTTPError,
        ConnectionError,
        TimeoutError,
        OSError,
    ),
)

retry_policy = RetryPolicy(max_attempts=2, base_delay=0.1, max_delay=0.5)


class OrderRequest(BaseModel):
    product_id: int = Field(default=1, ge=1)
    quantity: int = Field(default=1, ge=1)
    payment_method: str = Field(default="PIX")


configure_logging(
    service=os.environ.get("SERVICE_NAME", "order-service"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)

app = FastAPI(
    title="Order Service",
    description="Microsserviço de gestão e checkout de pedidos do e-commerce",
    version="0.1.0",
)
app.add_middleware(CorrelationIdMiddleware)

app.include_router(health_router)
register_timeout_handler(app)


# ---------- Handlers de Fallback ----------


def _recommendations_fallback() -> dict[str, Any]:
    return {
        "items": [],
        "degraded": True,
        "message": "Serviço de catálogo indisponível. Retornando lista vazia em modo degradado.",
    }


def _product_details_fallback(product_id: int) -> dict[str, Any]:
    cached = LOCAL_PRODUCT_CACHE.get(product_id)
    if cached is not None:
        return {
            **cached,
            "degraded": True,
            "source": "local_cache",
        }
    return {
        "id": product_id,
        "name": f"Produto #{product_id} (dados estendidos indisponíveis)",
        "price": 100.00,
        "in_stock": True,
        "degraded": True,
        "source": "fallback_default",
    }


def _enqueue_payment_for_background(envelope: dict[str, Any]) -> bool:
    """Enfileira o pagamento no RabbitMQ para processamento em background."""
    connection = None
    try:
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
        params = pika.ConnectionParameters(
            host=RABBITMQ_HOST,
            port=RABBITMQ_PORT,
            credentials=credentials,
            connection_attempts=1,
            socket_timeout=2.0,
        )
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        topology = Topology(
            exchange="orders",
            queue=PAYMENT_QUEUE,
            routing_key=PAYMENT_QUEUE,
        )
        declare_topology(channel, topology)
        body = json.dumps(envelope).encode("utf-8")
        props = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            message_id=str(envelope["event_id"]),
        )
        channel.basic_publish(
            exchange="orders",
            routing_key=PAYMENT_QUEUE,
            body=body,
            properties=props,
        )
        return True
    except Exception as exc:
        logger.warning(f"Não foi possível publicar no broker durante fallback: {exc}")
        return False
    finally:
        if connection is not None and connection.is_open:
            connection.close()


def _payment_fallback(order_payload: dict[str, Any]) -> dict[str, Any]:
    queued = _enqueue_payment_for_background(order_payload)
    return {
        "order_id": order_payload["order_id"],
        "event_id": order_payload["event_id"],
        "status": "PAYMENT_PENDING",
        "degraded": True,
        "queued_for_background": queued,
        "message": "Pagamento agendado para reprocessamento futuro em background.",
    }


# ---------- Operações protegidas por Resiliência ----------


@with_fallback(_recommendations_fallback)
async def fetch_catalog_recommendations() -> dict[str, Any]:
    async def _call() -> dict[str, Any]:
        async with DistributedHttpClient() as client:
            resp = await client.get_catalog(f"{CATALOG_SERVICE_URL}/recommendations")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return {**data, "degraded": False}

    res: dict[str, Any] = await retry_policy.call_async(
        catalog_breaker.call_async, _call
    )
    return res


@with_fallback(_product_details_fallback)
async def fetch_product_details(product_id: int) -> dict[str, Any]:
    async def _call() -> dict[str, Any]:
        async with DistributedHttpClient() as client:
            resp = await client.get_catalog(
                f"{CATALOG_SERVICE_URL}/products/{product_id}"
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            LOCAL_PRODUCT_CACHE[product_id] = data
            return {**data, "degraded": False, "source": "catalog-service"}

    return await retry_policy.call_async(catalog_breaker.call_async, _call)


@with_fallback(_payment_fallback)
async def request_payment_processing(order_payload: dict[str, Any]) -> dict[str, Any]:
    async def _call() -> dict[str, Any]:
        async with DistributedHttpClient() as client:
            resp = await client.authorize_payment(
                f"{PAYMENT_SERVICE_URL}/payments",
                json=order_payload,
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return {**data, "degraded": False}

    return await payment_breaker.call_async(_call)


# ---------- Rotas ----------


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


@app.get("/catalog/recommendations")
async def get_recommendations_endpoint() -> dict[str, Any]:
    return await fetch_catalog_recommendations()


@app.get("/catalog/products/{product_id}")
async def get_product_details_endpoint(product_id: int) -> dict[str, Any]:
    return await fetch_product_details(product_id)


@app.post("/payments")
async def create_payment_endpoint(order: OrderRequest) -> dict[str, Any]:
    order_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "event_id": event_id,
        "order_id": order_id,
        "product_id": order.product_id,
        "quantity": order.quantity,
        "amount": round(100.0 * order.quantity, 2),
        "payment_method": order.payment_method,
    }
    return await request_payment_processing(payload)


@app.post("/orders")
async def create_order(
    order: OrderRequest,
    user: UserIdentity = Depends(require_roles(["customer", "admin"])),
) -> dict[str, Any]:
    product = await fetch_product_details(order.product_id)
    logger.info(f"Pedido criado pelo usuário: {user.user_id}")
    unit_price = float(product.get("price", 100.0))
    order_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())

    envelope: dict[str, Any] = {
        "event_id": event_id,
        "order_id": order_id,
        "product_id": order.product_id,
        "quantity": order.quantity,
        "amount": round(unit_price * order.quantity, 2),
        "payment_method": order.payment_method,
    }

    payment_info = await request_payment_processing(envelope)
    degraded = bool(product.get("degraded") or payment_info.get("degraded"))

    return {
        "order_id": order_id,
        "event_id": event_id,
        "product": product,
        "quantity": order.quantity,
        "total_amount": envelope["amount"],
        "status": payment_info.get("status", "PAYMENT_PENDING"),
        "degraded": degraded,
        "payment": payment_info,
    }
