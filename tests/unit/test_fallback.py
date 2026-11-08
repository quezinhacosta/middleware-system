from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from middleware.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenException,
)
from middleware.resilience.exceptions import MiddlewareTimeoutError
from middleware.resilience.fallback import FallbackPolicy, with_fallback


def test_sucesso_sincrono_nao_aciona_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @with_fallback(lambda x: x * 10)
    def dobrar(x: int) -> int:
        return x * 2

    with caplog.at_level(logging.WARNING, logger="middleware.resilience.fallback"):
        assert dobrar(4) == 8

    assert "FALLBACK_TRIGGERED" not in caplog.text


def test_falha_sincrona_aciona_fallback_e_loga_alerta(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def contingencia(produto_id: int) -> dict[str, Any]:
        return {"id": produto_id, "degraded": True, "cached": True}

    @with_fallback(contingencia)
    def buscar_produto(produto_id: int) -> dict[str, Any]:
        raise ConnectionError("catalog-service recusou conexão")

    with caplog.at_level(logging.WARNING, logger="middleware.resilience.fallback"):
        resposta = buscar_produto(42)

    assert resposta == {"id": 42, "degraded": True, "cached": True}
    registros = [json.loads(r.message) for r in caplog.records]
    assert len(registros) == 1
    assert registros[0]["event"] == "FALLBACK_TRIGGERED"
    assert registros[0]["tag"] == "FALLBACK_TRIGGERED"
    assert registros[0]["reason"] == "ConnectionError"


def test_fallback_sem_argumentos_quando_funcao_recebe_parametros() -> None:
    @with_fallback(lambda: {"items": [], "degraded": True})
    def listar_recomendacoes(usuario_id: int) -> dict[str, Any]:
        raise TimeoutError(f"timeout para {usuario_id}")

    assert listar_recomendacoes(99) == {"items": [], "degraded": True}


@pytest.mark.asyncio
async def test_sucesso_assincrono_nao_aciona_fallback() -> None:
    @with_fallback(lambda: {"degraded": True})
    async def operacao_ok() -> dict[str, Any]:
        return {"degraded": False}

    assert await operacao_ok() == {"degraded": False}


@pytest.mark.asyncio
async def test_falha_assincrona_com_handler_assincrono(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler_async(order_id: str) -> dict[str, Any]:
        return {"order_id": order_id, "status": "PAYMENT_PENDING", "degraded": True}

    @with_fallback(handler_async)
    async def autorizar_pagamento(order_id: str) -> dict[str, Any]:
        raise MiddlewareTimeoutError(
            url="http://payment-service/pay", timeout=2.0, elapsed=2.1
        )

    with caplog.at_level(logging.WARNING, logger="middleware.resilience.fallback"):
        resultado = await autorizar_pagamento("ord-123")

    assert resultado == {
        "order_id": "ord-123",
        "status": "PAYMENT_PENDING",
        "degraded": True,
    }
    assert "FALLBACK_TRIGGERED" in caplog.text


@pytest.mark.asyncio
async def test_fallback_acionado_quando_circuit_breaker_abre(
    caplog: pytest.LogCaptureFixture,
) -> None:
    breaker = CircuitBreaker("catalog", failure_threshold=1, recovery_timeout=30.0)

    @with_fallback(lambda: {"items": [], "degraded": True})
    @breaker
    async def consultar_catalogo() -> dict[str, Any]:
        raise httpx.ConnectError("serviço fora do ar")

    with caplog.at_level(logging.WARNING, logger="middleware.resilience.fallback"):
        r1 = await consultar_catalogo()
        r2 = await consultar_catalogo()  # aqui o CircuitBreakerOpenException é lançado

    assert r1 == {"items": [], "degraded": True}
    assert r2 == {"items": [], "degraded": True}
    eventos = [
        json.loads(r.message)
        for r in caplog.records
        if r.name == "middleware.resilience.fallback"
    ]
    motivos = [e["reason"] for e in eventos]
    assert "ConnectError" in motivos
    assert "CircuitBreakerOpenException" in motivos


def test_excecao_nao_mapeada_propaga_sem_fallback() -> None:
    @with_fallback(lambda: "fallback", exceptions=(ConnectionError,))
    def erro_de_negocio() -> str:
        raise ValueError("CPF inválido")

    with pytest.raises(ValueError, match="CPF inválido"):
        erro_de_negocio()


def test_validacao_construtor_fallback_policy() -> None:
    with pytest.raises(TypeError):
        FallbackPolicy("nao_chamavel")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        FallbackPolicy(lambda: None, exceptions=())


def test_propriedades_fallback_policy() -> None:
    def handler() -> str:
        return "ok"

    policy = FallbackPolicy(handler, exceptions=(CircuitBreakerOpenException,))
    assert policy.fallback_handler is handler
    assert policy.exceptions == (CircuitBreakerOpenException,)


def test_rotas_order_service_retornam_http_200_com_degraded_quando_parceiros_caem(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import services.order_service.main as order_main

    monkeypatch.setattr(order_main, "_enqueue_payment_for_background", lambda env: True)

    client = TestClient(order_main.app)

    with caplog.at_level(logging.WARNING, logger="middleware.resilience.fallback"):
        resp_rec = client.get("/catalog/recommendations")
        resp_prod = client.get("/catalog/products/1")
        resp_pay = client.post(
            "/payments", json={"product_id": 1, "quantity": 2, "payment_method": "PIX"}
        )
        resp_order = client.post(
            "/orders", json={"product_id": 1, "quantity": 1, "payment_method": "PIX"}
        )

    assert resp_rec.status_code == 200
    assert resp_rec.json()["degraded"] is True
    assert resp_rec.json()["items"] == []

    assert resp_prod.status_code == 200
    assert resp_prod.json()["degraded"] is True
    assert resp_prod.json()["source"] == "local_cache"

    assert resp_pay.status_code == 200
    assert resp_pay.json()["degraded"] is True
    assert resp_pay.json()["status"] == "PAYMENT_PENDING"

    assert resp_order.status_code == 200
    assert resp_order.json()["degraded"] is True
    assert resp_order.json()["status"] == "PAYMENT_PENDING"
    assert "FALLBACK_TRIGGERED" in caplog.text
