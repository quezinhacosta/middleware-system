import asyncio

import httpx
import pytest

from middleware.http.distributed_http_client import DistributedHttpClient
from middleware.resilience.exceptions import MiddlewareTimeoutError


@pytest.fixture(autouse=True)
def limpar_envs(monkeypatch):
    for nome in ("TIMEOUT_CATALOG_SECONDS", "TIMEOUT_PAYMENT_SECONDS", "TIMEOUT_DEFAULT_SECONDS"):
        monkeypatch.delenv(nome, raising=False)


def fazer_cliente(handler):
    """Cliente com transporte simulado: nenhuma rede real é usada."""
    return DistributedHttpClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


# ---------- requisição básica ----------

async def test_request_retorna_a_resposta():
    capturado = {}

    def handler(request):
        capturado["metodo"] = request.method
        capturado["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    async with fazer_cliente(handler) as cliente:
        r = await cliente.request("GET", "http://catalog/items")

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert capturado == {"metodo": "GET", "url": "http://catalog/items"}


async def test_status_de_erro_nao_vira_excecao():
    async with fazer_cliente(lambda req: httpx.Response(500)) as cliente:
        r = await cliente.request("GET", "http://catalog/items")
    assert r.status_code == 500


# ---------- timeout por criticidade ----------

async def test_timeout_default_e_3_segundos():
    capturado = {}

    def handler(request):
        capturado.update(request.extensions["timeout"])
        return httpx.Response(200)

    async with fazer_cliente(handler) as cliente:
        await cliente.request("GET", "http://svc/x")
    assert capturado["read"] == 3.0
    assert capturado["connect"] == 3.0


@pytest.mark.parametrize(
    "metodo,esperado,verbo",
    [("get_catalog", 1.5, "GET"), ("authorize_payment", 5.0, "POST")],
)
async def test_atalhos_usam_timeout_da_criticidade(metodo, esperado, verbo):
    capturado = {}

    def handler(request):
        capturado["timeout"] = request.extensions["timeout"]["read"]
        capturado["verbo"] = request.method
        return httpx.Response(200)

    async with fazer_cliente(handler) as cliente:
        r = await getattr(cliente, metodo)("http://svc/x")

    assert r.status_code == 200
    assert capturado == {"timeout": esperado, "verbo": verbo}


async def test_timeout_respeita_env_var(monkeypatch):
    monkeypatch.setenv("TIMEOUT_CATALOG_SECONDS", "0.8")
    capturado = {}

    def handler(request):
        capturado["timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(200)

    async with fazer_cliente(handler) as cliente:
        await cliente.get_catalog("http://svc/x")
    assert capturado["timeout"] == 0.8


# ---------- estouro de prazo ----------

async def test_prazo_total_estourado_vira_middleware_timeout(monkeypatch):
    monkeypatch.setenv("TIMEOUT_CATALOG_SECONDS", "0.05")

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    async with fazer_cliente(handler) as cliente:
        with pytest.raises(MiddlewareTimeoutError) as exc:
            await cliente.get_catalog("http://catalog/x?token=abc")

    assert exc.value.timeout == 0.05
    assert exc.value.url == "http://catalog/x"  # query string removida
    assert "GET" in exc.value.operation


async def test_timeout_de_fase_do_httpx_vira_middleware_timeout():
    def handler(request):
        raise httpx.ReadTimeout("lento", request=request)

    async with fazer_cliente(handler) as cliente:
        with pytest.raises(MiddlewareTimeoutError) as exc:
            await cliente.get_catalog("http://catalog/x")
    assert exc.value.timeout == 1.5


async def test_timeout_de_fase_sanitiza_url():
    def handler(request):
        raise httpx.ReadTimeout("lento", request=request)

    async with fazer_cliente(handler) as cliente:
        with pytest.raises(MiddlewareTimeoutError) as exc:
            await cliente.get_catalog("http://catalog/x?token=abc")
    assert exc.value.url == "http://catalog/x"


# ---------- outros erros e ciclo de vida ----------

async def test_erro_de_conexao_nao_e_convertido():
    def handler(request):
        raise httpx.ConnectError("recusada", request=request)

    async with fazer_cliente(handler) as cliente:
        with pytest.raises(httpx.ConnectError):
            await cliente.request("GET", "http://catalog/x")


async def test_context_manager_fecha_o_client():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    async with DistributedHttpClient(client=http):
        assert not http.is_closed
    assert http.is_closed


async def test_client_padrao_tem_headers_e_nao_segue_redirect():
    async with DistributedHttpClient(default_headers={"X-Teste": "1"}) as cliente:
        assert cliente._client.headers["X-Teste"] == "1"
        assert cliente._client.follow_redirects is False
