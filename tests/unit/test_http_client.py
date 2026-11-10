import asyncio

import httpx
import pytest

from middleware.auth.context import set_auth_token
from middleware.http.distributed_http_client import DistributedHttpClient
from middleware.observability.correlation import CORRELATION_HEADER, correlation_id_var
from middleware.resilience.exceptions import (
    GatewayTimeoutException,
    MiddlewareTimeoutError,
    ServiceUnavailableException,
)


@pytest.fixture(autouse=True)
def limpar_envs(monkeypatch):
    for nome in (
        "TIMEOUT_CATALOG_SECONDS",
        "TIMEOUT_PAYMENT_SECONDS",
        "TIMEOUT_DEFAULT_SECONDS",
    ):
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


# ---------- métodos semânticos (get, post, put, delete) ----------


async def test_metodos_semanticos_get_post_put_delete():
    historico = []

    def handler(request):
        body = request.read().decode("utf-8")
        historico.append({
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": body,
        })
        return httpx.Response(200, json={"success": True})

    async with fazer_cliente(handler) as cliente:
        r_get = await cliente.get("http://svc/items", params={"cat": "eletronicos"})
        assert r_get.status_code == 200

        r_post = await cliente.post("http://svc/items", json={"name": "Teclado"})
        assert r_post.status_code == 200

        r_put = await cliente.put("http://svc/items/1", json={"price": 150.0})
        assert r_put.status_code == 200

        r_del = await cliente.delete("http://svc/items/1")
        assert r_del.status_code == 200

    assert historico[0]["method"] == "GET"
    assert "cat=eletronicos" in historico[0]["url"]

    assert historico[1]["method"] == "POST"
    assert '"name": "Teclado"' in historico[1]["body"]

    assert historico[2]["method"] == "PUT"
    assert '"price": 150.0' in historico[2]["body"]

    assert historico[3]["method"] == "DELETE"


# ---------- injeção automática de contexto e headers ----------


async def test_injecao_automatica_de_correlation_id_do_contexto():
    capturado = {}

    def handler(request):
        capturado["headers"] = dict(request.headers)
        return httpx.Response(200)

    token = correlation_id_var.set("req-corr-uuid-456")
    try:
        async with fazer_cliente(handler) as cliente:
            await cliente.get("http://svc/check")
    finally:
        correlation_id_var.reset(token)

    assert capturado["headers"].get(CORRELATION_HEADER.lower()) == "req-corr-uuid-456"


async def test_injecao_automatica_de_authorization_token_do_contexto():
    capturado = {}

    def handler(request):
        capturado["headers"] = dict(request.headers)
        return httpx.Response(200)

    set_auth_token("jwt-session-token-abc")
    try:
        async with fazer_cliente(handler) as cliente:
            await cliente.post("http://svc/resource", json={"a": 1})
    finally:
        set_auth_token(None)

    assert capturado["headers"].get("authorization") == "Bearer jwt-session-token-abc"


async def test_headers_explicitos_tem_precedencia_sobre_contexto():
    capturado = {}

    def handler(request):
        capturado["headers"] = dict(request.headers)
        return httpx.Response(200)

    token = correlation_id_var.set("cid-contexto")
    set_auth_token("token-contexto")
    try:
        async with fazer_cliente(handler) as cliente:
            await cliente.get(
                "http://svc/check",
                headers={
                    "X-Correlation-ID": "cid-customizado",
                    "Authorization": "Bearer token-customizado",
                },
            )
    finally:
        correlation_id_var.reset(token)
        set_auth_token(None)

    assert capturado["headers"].get(CORRELATION_HEADER.lower()) == "cid-customizado"
    assert capturado["headers"].get("authorization") == "Bearer token-customizado"


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


# ---------- mapeamento de exceções padronizadas de transporte ----------


async def test_erro_de_conexao_recusada_vira_service_unavailable():
    def handler(request):
        raise httpx.ConnectError("recusada", request=request)

    async with fazer_cliente(handler) as cliente:
        with pytest.raises(ServiceUnavailableException) as exc:
            await cliente.request("GET", "http://catalog/x")
        assert "catalog/x" in str(exc.value)
        assert isinstance(exc.value, ConnectionError)


async def test_timeout_de_conexao_vira_gateway_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("timeout de conexao", request=request)

    async with fazer_cliente(handler) as cliente:
        with pytest.raises(GatewayTimeoutException) as exc:
            await cliente.get("http://payment/pay")
        assert exc.value.url == "http://payment/pay"
        assert isinstance(exc.value, MiddlewareTimeoutError)


async def test_erro_de_socket_network_vira_service_unavailable():
    def handler(request):
        raise httpx.NetworkError("falha de rota", request=request)

    async with fazer_cliente(handler) as cliente:
        with pytest.raises(ServiceUnavailableException) as exc:
            await cliente.post("http://catalog/item", json={"x": 1})
        assert isinstance(exc.value, ConnectionError)


# ---------- ciclo de vida do cliente ----------


async def test_context_manager_fecha_o_client():
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    async with DistributedHttpClient(client=http):
        assert not http.is_closed
    assert http.is_closed


async def test_client_padrao_tem_headers_e_nao_segue_redirect():
    async with DistributedHttpClient(default_headers={"X-Teste": "1"}) as cliente:
        assert cliente._client.headers["X-Teste"] == "1"
        assert cliente._client.follow_redirects is False
