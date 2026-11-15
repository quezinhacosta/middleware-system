import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.http.distributed_http_client import DistributedHttpClient
from middleware.resilience.exceptions import MiddlewareTimeoutError
from middleware.resilience.handlers import register_timeout_handler
from middleware.resilience.timeout import with_timeout
from middleware.resilience.timeout_config import Criticality


@pytest.mark.asyncio
async def test_async_call_is_cancelled_at_deadline():
    cancelled = asyncio.Event()

    @with_timeout(1.0)
    async def slow(url: str):
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    start = time.perf_counter()
    with pytest.raises(MiddlewareTimeoutError) as exc:
        await slow("http://catalog/products/1")
    elapsed = time.perf_counter() - start

    assert 1.0 <= elapsed < 1.5  # não esperou os 3s
    assert cancelled.is_set()  # corrotina foi realmente cancelada
    assert exc.value.url == "http://catalog/products/1"


def test_sync_call_returns_control_at_deadline():
    @with_timeout(1.0)
    def slow():
        time.sleep(3)

    start = time.perf_counter()
    with pytest.raises(MiddlewareTimeoutError):
        slow()
    assert time.perf_counter() - start < 1.5


def test_fast_call_is_unaffected():
    @with_timeout(1.0)
    def fast():
        return "ok"

    assert fast() == "ok"


def test_route_returns_504_with_json_log(caplog):
    app = FastAPI()
    register_timeout_handler(app)

    @with_timeout(1.0)
    async def call_catalog(url: str):
        await asyncio.sleep(3)

    @app.get("/slow")
    async def slow_route():
        await call_catalog("http://catalog:8000/products/1?token=secret")

    with caplog.at_level("ERROR", logger="middleware.resilience.timeout"):
        start = time.perf_counter()
        resp = TestClient(app).get("/slow")
        elapsed = time.perf_counter() - start

    assert resp.status_code == 504
    assert resp.json()["error"] == "gateway_timeout"
    assert 0.9 <= elapsed < 1.5

    log = json.loads(caplog.records[-1].message)
    assert log["event"] == "middleware_timeout"
    assert log["url"] == "http://catalog:8000/products/1"
    assert 900 <= log["duration_ms"] < 1500


@pytest.mark.asyncio
async def test_limit_read_from_env(monkeypatch):
    monkeypatch.setenv("TIMEOUT_CATALOG_SECONDS", "0.2")

    @with_timeout(Criticality.CATALOG)
    async def slow():
        await asyncio.sleep(1)

    start = time.perf_counter()
    with pytest.raises(MiddlewareTimeoutError):
        await slow()
    assert time.perf_counter() - start < 0.6


@pytest.mark.asyncio
async def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TIMEOUT_PAYMENT_SECONDS", "abc")

    @with_timeout(Criticality.PAYMENT)
    async def fast():
        return "ok"

    assert await fast() == "ok"  # default 5.0s, sem quebrar


@pytest.mark.asyncio
async def test_http_client_aborts_slow_request():
    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3)  # serviço lento simulado
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(slow_handler)
    http = DistributedHttpClient(client=httpx.AsyncClient(transport=transport))

    start = time.perf_counter()
    async with http:
        with pytest.raises(MiddlewareTimeoutError) as exc:
            await http.request(
                "GET", "http://catalog:8000/products/1", timeout_override=None
            ) if False else await http.get_catalog("http://catalog:8000/products/1")
    assert time.perf_counter() - start < 2.0  # catálogo = 1.5s, não 3s
    assert exc.value.url == "http://catalog:8000/products/1"
