"""Métricas Prometheus: requisições HTTP, latência e mensagens de fila.

Cada serviço cria um ``MetricsCollector`` com registro próprio, o que evita
conflito de nomes entre instâncias (útil nos testes). O endpoint ``/metrics``
também exporta o registro padrão do ``prometheus_client``, onde ficam o estado
do circuit breaker (``circuit_breaker_state``) e as métricas de processo
(CPU e memória).
"""

import time

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
UNMATCHED_ROUTE = "unmatched"


class MetricsCollector:
    """Agrupa as métricas do middleware HTTP e dos consumidores de fila."""

    def __init__(self, service: str, registry: CollectorRegistry | None = None) -> None:
        self.service = service
        self.registry = registry or CollectorRegistry()

        self.http_requests_total = Counter(
            "http_requests_total",
            "Total de requisições HTTP atendidas",
            ["method", "endpoint", "status_code", "service"],
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            "http_request_duration_seconds",
            "Duração das requisições HTTP em segundos",
            ["method", "endpoint", "service"],
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.queue_messages_processed_total = Counter(
            "queue_messages_processed_total",
            "Mensagens processadas das filas",
            ["queue_name", "status"],
            registry=self.registry,
        )

    def observe_http(
        self, method: str, endpoint: str, status_code: int, duration: float
    ) -> None:
        self.http_requests_total.labels(
            method=method,
            endpoint=endpoint,
            status_code=str(status_code),
            service=self.service,
        ).inc()
        self.http_request_duration_seconds.labels(
            method=method, endpoint=endpoint, service=self.service
        ).observe(duration)

    def observe_message(self, queue_name: str, status: str) -> None:
        self.queue_messages_processed_total.labels(
            queue_name=queue_name, status=status
        ).inc()


class MetricsMiddleware:
    """Middleware ASGI que registra contagem e latência de cada requisição HTTP."""

    def __init__(self, app: ASGIApp, collector: MetricsCollector) -> None:
        self.app = app
        self.collector = collector

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500
        started = time.perf_counter()

        async def send_capturing_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capturing_status)
        finally:
            duration = time.perf_counter() - started
            route = scope.get("route")
            endpoint = getattr(route, "path", UNMATCHED_ROUTE)
            self.collector.observe_http(
                method=scope["method"],
                endpoint=endpoint,
                status_code=status_code,
                duration=duration,
            )


def build_metrics_router(collector: MetricsCollector) -> APIRouter:
    """Expõe ``GET /metrics`` no formato de texto do Prometheus."""
    router = APIRouter(tags=["metrics"])

    @router.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        body = generate_latest(collector.registry) + generate_latest(REGISTRY)
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    return router
