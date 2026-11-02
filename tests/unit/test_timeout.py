import asyncio
import contextvars
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.resilience.exceptions import MiddlewareTimeoutError
from middleware.resilience.handlers import register_timeout_handler
from middleware.resilience.timeout import (
    _extract_url,
    _sanitize_url,
    run_with_deadline,
    with_timeout,
)
from middleware.resilience.timeout_config import Criticality, get_timeout

# ---------- timeout_config ----------

ENVS = ["TIMEOUT_CATALOG_SECONDS", "TIMEOUT_PAYMENT_SECONDS", "TIMEOUT_DEFAULT_SECONDS"]


@pytest.fixture(autouse=True)
def limpar_envs(monkeypatch):
    for nome in ENVS:
        monkeypatch.delenv(nome, raising=False)


def test_defaults():
    assert get_timeout(Criticality.CATALOG) == 1.5
    assert get_timeout(Criticality.PAYMENT) == 5.0
    assert get_timeout(Criticality.DEFAULT) == 3.0
    assert get_timeout() == 3.0


def test_env_sobrescreve_default(monkeypatch):
    monkeypatch.setenv("TIMEOUT_PAYMENT_SECONDS", "9.5")
    assert get_timeout(Criticality.PAYMENT) == 9.5


@pytest.mark.parametrize("valor", ["abc", "0", "-1", "nan", "inf", ""])
def test_env_invalida_cai_no_default(monkeypatch, valor):
    monkeypatch.setenv("TIMEOUT_CATALOG_SECONDS", valor)
    assert get_timeout(Criticality.CATALOG) == 1.5


# ---------- helpers ----------

def test_sanitize_url_remove_query_e_credenciais():
    url = "https://user:senha@catalog:8080/items/1?token=segredo"
    assert _sanitize_url(url) == "https://catalog:8080/items/1"


def test_sanitize_url_none_e_vazio():
    assert _sanitize_url(None) is None
    assert _sanitize_url("") is None


def test_extract_url_de_kwargs_e_args():
    assert _extract_url((), {"url": "http://a/b"}) == "http://a/b"
    assert _extract_url(("x", "https://a/b"), {}) == "https://a/b"
    assert _extract_url(("x", 1), {}) is None


def test_mensagem_da_excecao():
    exc = MiddlewareTimeoutError(url="http://a/b", timeout=1.5, elapsed=1.6)
    assert "1.50s" in str(exc) and "http://a/b" in str(exc)
    assert exc.timeout == 1.5


# ---------- run_with_deadline ----------

async def test_run_with_deadline_sucesso():
    async def rapida():
        return 42

    assert await run_with_deadline(rapida(), 1) == 42


async def test_run_with_deadline_estoura_e_sanitiza_url():
    async def lenta():
        await asyncio.sleep(1)

    with pytest.raises(MiddlewareTimeoutError) as exc:
        await run_with_deadline(lenta(), 0.05, url="http://catalog/x?token=abc", operation="buscar")
    assert exc.value.url == "http://catalog/x"
    assert exc.value.operation == "buscar"
    assert exc.value.timeout == 0.05
    assert exc.value.elapsed >= 0.04


# ---------- with_timeout (async) ----------

async def test_with_timeout_async_sucesso():
    @with_timeout(1)
    async def f(x):
        return x * 2

    assert await f(2) == 4


async def test_with_timeout_async_estoura():
    @with_timeout(0.05)
    async def lenta(url):
        await asyncio.sleep(1)

    with pytest.raises(MiddlewareTimeoutError) as exc:
        await lenta(url="http://catalog/x?token=abc")
    assert exc.value.url == "http://catalog/x"
    assert "lenta" in exc.value.operation


async def test_with_timeout_usa_criticality_da_env(monkeypatch):
    monkeypatch.setenv("TIMEOUT_CATALOG_SECONDS", "0.05")

    @with_timeout(Criticality.CATALOG)
    async def lenta():
        await asyncio.sleep(1)

    with pytest.raises(MiddlewareTimeoutError) as exc:
        await lenta()
    assert exc.value.timeout == 0.05


async def test_with_timeout_segundos_invalidos():
    @with_timeout(0)
    async def f():
        return 1

    with pytest.raises(ValueError):
        await f()


# ---------- with_timeout (sync) ----------

def test_with_timeout_sync_sucesso():
    @with_timeout(1)
    def f(x):
        return x + 1

    assert f(1) == 2


def test_with_timeout_sync_estoura():
    @with_timeout(0.05)
    def lenta():
        time.sleep(0.4)

    with pytest.raises(MiddlewareTimeoutError):
        lenta()


def test_with_timeout_sync_propaga_excecao_da_funcao():
    @with_timeout(1)
    def f():
        raise KeyError("x")

    with pytest.raises(KeyError):
        f()


def test_with_timeout_sync_timeouterror_da_propria_funcao_nao_vira_middleware():
    @with_timeout(1)
    def f():
        raise TimeoutError("interno")

    with pytest.raises(TimeoutError) as exc:
        f()
    assert not isinstance(exc.value, MiddlewareTimeoutError)


def test_with_timeout_sync_propaga_contextvars():
    var = contextvars.ContextVar("cid", default="nenhum")
    var.set("abc-123")

    @with_timeout(1)
    def le():
        return var.get()

    assert le() == "abc-123"


def test_with_timeout_preserva_nome():
    @with_timeout(1)
    def minha_funcao():
        pass

    assert minha_funcao.__name__ == "minha_funcao"


# ---------- handler 504 ----------

def test_handler_retorna_504_sem_vazar_url():
    app = FastAPI()
    register_timeout_handler(app)

    @app.get("/lenta")
    def lenta():
        raise MiddlewareTimeoutError(url="http://interno/segredo", timeout=1.5, elapsed=1.6)

    r = TestClient(app).get("/lenta")
    assert r.status_code == 504
    body = r.json()
    assert body["error"] == "gateway_timeout"
    assert body["timeout_seconds"] == 1.5
    assert "interno" not in r.text
