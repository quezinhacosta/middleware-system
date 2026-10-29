"""Métricas Prometheus: requisições HTTP, latência, estado de circuit breaker e filas.

Cada serviço cria um ``MetricsCollector`` com o próprio registro, o que evita
conflito de nomes entre instâncias (útil nos testes).
"""

import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
UNMATCHED_ROUTE = "unmatched"


class MetricsCollector:
    """Agrupa as métricas do middleware e dos consumidores de fila."""

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
        self.circuit_breaker_state = Gauge(
            "circuit_breaker_state",
            "Estado do circuit breaker por destino: 0 fechado, 1 aberto",
            ["service_target"],
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

    def set_circuit_state(self, service_target: str, is_open: bool) -> None:
        self.circuit_breaker_state.labels(service_target=service_target).set(
            1 if is_open else 0
        )


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
