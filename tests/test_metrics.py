from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from middleware.observability.metrics import MetricsCollector, MetricsMiddleware


def build_app(collector: MetricsCollector) -> FastAPI:
    app = FastAPI()
    app.add_middleware(MetricsMiddleware, collector=collector)

    @app.get("/orders/{order_id}")
    def get_order(order_id: int) -> dict[str, int]:
        return {"id": order_id}

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("falha de teste")

    return app


def make_collector() -> MetricsCollector:
    return MetricsCollector(service="order-service", registry=CollectorRegistry())


def test_conta_requisicao_com_template_da_rota() -> None:
    collector = make_collector()
    client = TestClient(build_app(collector))

    client.get("/orders/1")
    client.get("/orders/2")

    labels = {
        "method": "GET",
        "endpoint": "/orders/{order_id}",
        "status_code": "200",
        "service": "order-service",
    }
    value = collector.registry.get_sample_value("http_requests_total", labels)
    assert value == 2


def test_registra_duracao_no_histograma() -> None:
    collector = make_collector()
    client = TestClient(build_app(collector))

    client.get("/orders/1")

    labels = {
        "method": "GET",
        "endpoint": "/orders/{order_id}",
        "service": "order-service",
    }
    count = collector.registry.get_sample_value(
        "http_request_duration_seconds_count", labels
    )
    assert count == 1


def test_rota_inexistente_usa_label_unmatched() -> None:
    collector = make_collector()
    client = TestClient(build_app(collector))

    client.get("/nao-existe")

    labels = {
        "method": "GET",
        "endpoint": "unmatched",
        "status_code": "404",
        "service": "order-service",
    }
    assert collector.registry.get_sample_value("http_requests_total", labels) == 1


def test_erro_nao_tratado_conta_como_500() -> None:
    collector = make_collector()
    client = TestClient(build_app(collector), raise_server_exceptions=False)

    client.get("/boom")

    labels = {
        "method": "GET",
        "endpoint": "/boom",
        "status_code": "500",
        "service": "order-service",
    }
    assert collector.registry.get_sample_value("http_requests_total", labels) == 1


def test_estado_do_circuit_breaker_alterna_entre_0_e_1() -> None:
    collector = make_collector()
    labels = {"service_target": "catalog-service"}

    collector.set_circuit_state("catalog-service", is_open=True)
    assert collector.registry.get_sample_value("circuit_breaker_state", labels) == 1

    collector.set_circuit_state("catalog-service", is_open=False)
    assert collector.registry.get_sample_value("circuit_breaker_state", labels) == 0


def test_conta_mensagens_processadas_por_fila() -> None:
    collector = make_collector()

    collector.observe_message("payment.approved", "ok")
    collector.observe_message("payment.approved", "ok")
    collector.observe_message("payment.approved", "error")

    ok = collector.registry.get_sample_value(
        "queue_messages_processed_total",
        {"queue_name": "payment.approved", "status": "ok"},
    )
    error = collector.registry.get_sample_value(
        "queue_messages_processed_total",
        {"queue_name": "payment.approved", "status": "error"},
    )
    assert ok == 2
    assert error == 1
