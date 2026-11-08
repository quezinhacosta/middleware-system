import logging
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.observability.correlation import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    correlation_headers,
    get_correlation_id,
)
from middleware.observability.logging import CorrelationIdFilter


def build_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/eco")
    def eco() -> dict[str, str | None]:
        return {"correlation_id": get_correlation_id()}

    return TestClient(app)


def test_gera_uuid_quando_header_ausente() -> None:
    response = build_client().get("/eco")

    generated = response.headers[CORRELATION_HEADER]
    assert str(uuid.UUID(generated, version=4)) == generated
    assert response.json() == {"correlation_id": generated}


def test_preserva_id_recebido_no_header() -> None:
    response = build_client().get("/eco", headers={CORRELATION_HEADER: "abc-123"})

    assert response.headers[CORRELATION_HEADER] == "abc-123"
    assert response.json() == {"correlation_id": "abc-123"}


def test_substitui_id_invalido_por_novo_uuid() -> None:
    response = build_client().get("/eco", headers={CORRELATION_HEADER: "x" * 300})

    generated = response.headers[CORRELATION_HEADER]
    assert generated != "x" * 300
    uuid.UUID(generated)


def test_contexto_e_limpo_apos_a_requisicao() -> None:
    client = build_client()
    client.get("/eco", headers={CORRELATION_HEADER: "abc-123"})

    assert get_correlation_id() is None


def test_correlation_headers_propaga_id_da_requisicao_atual() -> None:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/saida")
    def saida() -> dict[str, dict[str, str]]:
        return {"headers": correlation_headers()}

    response = TestClient(app).get("/saida", headers={CORRELATION_HEADER: "xyz-9"})

    assert response.json() == {"headers": {CORRELATION_HEADER: "xyz-9"}}


def test_correlation_headers_vazio_fora_de_requisicao() -> None:
    assert correlation_headers() == {}


def test_log_dentro_da_requisicao_recebe_o_correlation_id() -> None:
    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("tests.correlation")
    handler = ListHandler()
    handler.addFilter(CorrelationIdFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/log")
    def log() -> dict[str, str]:
        logger.info("dentro da requisicao")
        return {"status": "ok"}

    try:
        TestClient(app).get("/log", headers={CORRELATION_HEADER: "log-42"})
    finally:
        logger.removeHandler(handler)

    assert records[-1].__dict__["correlation_id"] == "log-42"
