import json
import logging
import sys
from collections.abc import Iterator

import pytest

from middleware.observability.logging import (
    CorrelationIdFilter,
    JsonLogFormatter,
    configure_logging,
    correlation_id_var,
)


def make_record(
    message: str = "evento",
    level: int = logging.INFO,
    **extra: object,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="orders.controller",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_emite_json_com_campos_obrigatorios() -> None:
    formatter = JsonLogFormatter(service="order-service")

    payload = json.loads(formatter.format(make_record("Pedido criado")))

    assert payload["level"] == "INFO"
    assert payload["service"] == "order-service"
    assert payload["logger"] == "orders.controller"
    assert payload["message"] == "Pedido criado"
    assert payload["timestamp"].endswith("Z")


def test_formatter_inclui_campos_extras_de_http() -> None:
    formatter = JsonLogFormatter(service="order-service")
    record = make_record(
        http_method="POST",
        http_path="/orders",
        duration_ms=45.2,
        status_code=201,
    )

    payload = json.loads(formatter.format(record))

    assert payload["http_method"] == "POST"
    assert payload["http_path"] == "/orders"
    assert payload["duration_ms"] == 45.2
    assert payload["status_code"] == 201


def test_formatter_stack_trace_nao_quebra_a_linha_json() -> None:
    formatter = JsonLogFormatter(service="order-service")
    try:
        raise RuntimeError("falha de teste\ncom quebra de linha")
    except RuntimeError:
        record = make_record("erro", level=logging.ERROR)
        record.exc_info = sys.exc_info()

    line = formatter.format(record)
    payload = json.loads(line)

    assert "\n" not in line
    assert "RuntimeError" in payload["stack_trace"]


def test_correlation_id_do_contexto_aparece_no_log() -> None:
    formatter = JsonLogFormatter(service="order-service")
    token = correlation_id_var.set("c7a8b9e0")
    try:
        record = make_record()
        CorrelationIdFilter().filter(record)
        payload = json.loads(formatter.format(record))
    finally:
        correlation_id_var.reset(token)

    assert payload["correlation_id"] == "c7a8b9e0"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


def test_configure_logging_aplica_formato_json_ao_root(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(service="catalog-service", level="INFO")

    logging.getLogger("catalog").info("item carregado")

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["service"] == "catalog-service"
    assert payload["message"] == "item carregado"
