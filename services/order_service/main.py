import json
import logging
import os
import sys
import uuid
from typing import Any

import httpx
import pika
from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import ValidationError

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
from services.order_service.models import (
    Order,
    OrderCreateRequest,
    OrderItem,
    OrderStatus,
)
from services.order_service.repository import OrderRepository

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

# Repositório em memória thread-safe para armazenar pedidos
order_repository = OrderRepository()

# Cache local de contingência para produtos quando o Catalog Service estiver indisponível
LOCAL_PRODUCT_CACHE: dict[str, dict[str, Any]] = {
    "1": {
        "id": "1",
        "name": "Notebook Pro 16 (Cache Local)",
        "price": 7500.00,
        "in_stock": True,
    },
    "2": {
        "id": "2",
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


def _product_details_fallback(product_id: int | str) -> dict[str, Any]:
    pid_str = str(product_id)
    cached = LOCAL_PRODUCT_CACHE.get(pid_str)
    if cached is not None:
        return {
            **cached,
            "degraded": True,
            "source": "local_cache",
        }
    return {
        "id": pid_str,
        "name": f"Produto #{pid_str} (dados estendidos indisponíveis)",
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
async def fetch_product_details(product_id: int | str) -> dict[str, Any]:
    pid_str = str(product_id)

    async def _call() -> dict[str, Any]:
        async with DistributedHttpClient() as client:
            resp = await client.get_catalog(
                f"{CATALOG_SERVICE_URL}/products/{pid_str}"
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            LOCAL_PRODUCT_CACHE[pid_str] = data
            return {**data, "degraded": False, "source": "catalog-service"}

    return await retry_policy.call_async(catalog_breaker.call_async, _call)


async def reserve_catalog_stock(product_id: str, quantity: int) -> dict[str, Any]:
    """Chama o endpoint de reserva de estoque no Catalog Service via DistributedHttpClient."""
    async with DistributedHttpClient() as client:
        resp = await client.request(
            "POST",
            f"{CATALOG_SERVICE_URL}/products/{product_id}/reserve",
            json={"quantity": quantity},
        )
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Produto {product_id} não encontrado no catálogo",
            )
        if resp.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Estoque insuficiente no catálogo para o produto {product_id}",
            )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data


async def release_catalog_stock(product_id: str, quantity: int) -> dict[str, Any] | None:
    """Compensa/estorna reserva de estoque no Catalog Service via DistributedHttpClient (SAGA)."""
    try:
        async with DistributedHttpClient() as client:
            resp = await client.request(
                "POST",
                f"{CATALOG_SERVICE_URL}/products/{product_id}/release",
                json={"quantity": quantity},
            )
            if resp.is_success:
                data: dict[str, Any] = resp.json()
                return data
            logger.warning(
                "Falha ao liberar estoque do produto %s (status %s): %s",
                product_id,
                resp.status_code,
                resp.text,
            )
    except Exception as exc:
        logger.error(
            "Erro inesperado ao compensar estoque do produto %s: %s",
            product_id,
            exc,
        )
    return None


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


@app.get("/catalog/recommendations")
async def get_recommendations_endpoint() -> dict[str, Any]:
    return await fetch_catalog_recommendations()


@app.get("/catalog/products/{product_id}")
async def get_product_details_endpoint(product_id: int) -> dict[str, Any]:
    return await fetch_product_details(product_id)


@app.post("/payments")
async def create_payment_endpoint(order: OrderCreateRequest) -> dict[str, Any]:
    order_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    pid = str(order.product_id or 1)
    payload: dict[str, Any] = {
        "event_id": event_id,
        "order_id": order_id,
        "product_id": pid,
        "quantity": order.quantity,
        "amount": round(100.0 * order.quantity, 2),
        "payment_method": order.payment_method,
    }
    return await request_payment_processing(payload)


@app.get("/orders", response_model=list[Order])
def list_orders(
    user: UserIdentity = Depends(require_roles(["customer", "admin"])),
) -> list[Order]:
    """Lista os pedidos do usuário autenticado (ou todos se for admin)."""
    if "admin" in user.roles:
        return order_repository.list_all()
    return order_repository.list_by_customer(user.user_id)


@app.get("/orders/{order_id}", response_model=Order)
def get_order_by_id(
    order_id: str,
    user: UserIdentity = Depends(require_roles(["customer", "admin"])),
) -> Order:
    """Consulta os detalhes e situação atual de um pedido específico."""
    order = order_repository.get_by_id(order_id)
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pedido {order_id} não encontrado",
        )
    if "admin" not in user.roles and order.customer_id != user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso não autorizado ao pedido solicitado",
        )
    return order


@app.post("/orders")
async def create_order(
    order_in: OrderCreateRequest,
    user: UserIdentity = Depends(require_roles(["customer", "admin"])),
) -> dict[str, Any]:
    """Cria um novo pedido validando disponibilidade no catálogo e publicando evento."""
    # Normaliza lista de itens
    items: list[OrderItem] = []
    if order_in.items:
        items = order_in.items
    else:
        pid = str(order_in.product_id or "1")
        items = [OrderItem(product_id=pid, quantity=order_in.quantity, unit_price=0.0)]

    # 1. Validação com o Catalog Service via middleware síncrono (reserva de estoque)
    total_amount = 0.0
    reserved_items: list[tuple[str, int]] = []
    try:
        for item in items:
            # Reserva o item no catálogo e obtém os dados do produto diretamente na resposta
            try:
                reservation = await reserve_catalog_stock(item.product_id, item.quantity)
                item.unit_price = float(reservation.get("unit_price") or 100.0)
                reserved_items.append((item.product_id, item.quantity))
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning(
                    "Falha ao contatar catalog-service diretamente para reserva: %s", exc
                )
                # Em modo fallback, consulta cache ou usa valor padrão
                try:
                    prod_details = await fetch_product_details(item.product_id)
                    item.unit_price = float(prod_details.get("price", 100.0))
                except Exception:
                    item.unit_price = 100.0

            total_amount += item.unit_price * item.quantity
    except Exception:
        # Transação Compensatória (SAGA): se falhar em qualquer item posterior,
        # estorna o estoque dos itens que já haviam sido reservados
        for res_pid, res_qty in reserved_items:
            await release_catalog_stock(res_pid, res_qty)
        raise

    # 2. Ao ser aceito, o pedido recebe status PENDING e ID UUID v4
    order_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())

    new_order = Order(
        id=order_id,
        customer_id=user.user_id,
        items=items,
        total_amount=round(total_amount, 2),
        status=OrderStatus.PENDING,
        payment_method=order_in.payment_method,
    )
    # Persiste no repositório thread-safe
    order_repository.save(new_order)
    logger.info("Pedido %s criado com status PENDING para usuário %s", order_id, user.user_id)

    # 3. Dispara evento de pagamento (assíncrono no broker com fallback)
    envelope: dict[str, Any] = {
        "event_id": event_id,
        "order_id": order_id,
        "product_id": items[0].product_id,
        "quantity": items[0].quantity,
        "amount": round(total_amount, 2),
        "payment_method": order_in.payment_method,
    }

    payment_info = await request_payment_processing(envelope)
    degraded = bool(payment_info.get("degraded"))

    return {
        "order_id": order_id,
        "event_id": event_id,
        "customer_id": user.user_id,
        "items": [item.model_dump() for item in items],
        "total_amount": round(total_amount, 2),
        "status": new_order.status.value,
        "degraded": degraded,
        "payment": payment_info,
    }
