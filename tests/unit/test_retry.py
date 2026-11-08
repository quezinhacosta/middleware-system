import asyncio
import json
import logging

import httpx
import pytest

from middleware.http.distributed_http_client import DistributedHttpClient
from middleware.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenException,
)
from middleware.resilience.exceptions import MiddlewareTimeoutError
from middleware.resilience.retry import RetryPolicy

LOGGER = "middleware.resilience.retry"


class Abortar(BaseException):
    """Simula KeyboardInterrupt (não é Exception)."""


class Sequencia:
    """Chamável de teste: devolve/lança os itens em ordem; repete o último."""

    def __init__(self, *itens):
        self.itens = list(itens)
        self.chamadas = 0

    def _proximo(self):
        self.chamadas += 1
        item = self.itens.pop(0) if len(self.itens) > 1 else self.itens[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def __call__(self, *args, **kwargs):
        return self._proximo()

    async def acall(self, *args, **kwargs):
        return self._proximo()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def factory(sleeps):
    async def async_sleep(segundos):
        sleeps.append(segundos)

    def _make(**kw):
        kw.setdefault("sleep", sleeps.append)
        kw.setdefault("async_sleep", async_sleep)
        kw.setdefault("random_func", lambda: 0.0)  # sem jitter por padrão
        return RetryPolicy(**kw)

    return _make


def eventos(caplog, nivel=None):
    registros = [r for r in caplog.records if r.name == LOGGER]
    if nivel is not None:
        registros = [r for r in registros if r.levelno == nivel]
    return [json.loads(r.getMessage()) for r in registros]


# ---------- configuração ----------


def test_padroes():
    policy = RetryPolicy()
    assert policy.max_attempts == 3
    assert policy.base_delay == 0.5
    assert policy.max_delay == 5.0


@pytest.mark.parametrize(
    "kw",
    [
        {"max_attempts": 0},
        {"base_delay": -1},
        {"max_delay": -1},
        {"base_delay": 2, "max_delay": 1},
        {"jitter_ratio": -0.1},
        {"jitter_ratio": 1.5},
    ],
)
def test_parametros_invalidos(kw):
    with pytest.raises(ValueError):
        RetryPolicy(**kw)


@pytest.mark.parametrize("codigo", [400, 401, 404, 429, 200, 600])
def test_codigos_nao_5xx_sao_recusados(codigo):
    with pytest.raises(ValueError):
        RetryPolicy(retryable_status_codes={503, codigo})


# ---------- cálculo do atraso e jitter ----------


def test_backoff_exponencial_com_teto(factory):
    policy = factory(jitter_ratio=0)
    assert [policy.compute_delay(a) for a in range(1, 6)] == [0.5, 1.0, 2.0, 4.0, 5.0]


def test_tentativa_absurda_nao_estoura(factory):
    assert factory(jitter_ratio=0).compute_delay(10_000) == 5.0


def test_attempt_invalido(factory):
    with pytest.raises(ValueError):
        factory().compute_delay(0)


def test_jitter_maximo_e_proporcional_ao_atraso(factory):
    policy = factory(random_func=lambda: 1.0)  # pior caso: jitter máximo (25%)
    assert policy.compute_delay(1) == pytest.approx(0.625)
    assert policy.compute_delay(2) == pytest.approx(1.25)


def test_atrasos_continuam_crescentes_mesmo_com_jitter_maximo(factory):
    policy = factory(random_func=lambda: 1.0)
    atrasos = [policy.compute_delay(a) for a in (1, 2, 3)]
    assert atrasos == sorted(atrasos)
    assert len(set(atrasos)) == 3


def test_jitter_somado_depois_do_teto_limitado_pelo_ratio(factory):
    policy = factory(random_func=lambda: 1.0)
    assert policy.compute_delay(10) == pytest.approx(5.0 * 1.25)


def test_jitter_aleatorio_fica_na_faixa_e_varia():
    policy = RetryPolicy()
    valores = [policy.compute_delay(2) for _ in range(500)]
    assert all(1.0 <= v <= 1.25 for v in valores)
    assert len(set(valores)) > 1


# ---------- call (sync): sucesso e retentativas ----------


def test_sucesso_na_primeira_nao_espera(factory, sleeps):
    func = Sequencia("ok")
    assert factory().call(func) == "ok"
    assert func.chamadas == 1
    assert sleeps == []


def test_falhas_transitorias_seguidas_de_sucesso(factory, sleeps):
    func = Sequencia(ConnectionError("x"), ConnectionError("x"), "ok")
    assert factory().call(func) == "ok"
    assert func.chamadas == 3
    assert sleeps == [0.5, 1.0]  # intervalos crescentes


def test_esgota_as_tentativas_e_relanca(factory, sleeps):
    func = Sequencia(ConnectionError("x"))
    with pytest.raises(ConnectionError):
        factory().call(func)
    assert func.chamadas == 3
    assert len(sleeps) == 2


def test_max_attempts_1_nao_retenta(factory, sleeps):
    func = Sequencia(ConnectionError("x"))
    with pytest.raises(ConnectionError):
        factory(max_attempts=1).call(func)
    assert func.chamadas == 1
    assert sleeps == []


def test_max_attempts_4_gera_tres_esperas_crescentes(factory, sleeps):
    func = Sequencia(ConnectionError("x"))
    with pytest.raises(ConnectionError):
        factory(max_attempts=4).call(func)
    assert sleeps == [0.5, 1.0, 2.0]


def test_repassa_argumentos(factory):
    assert factory().call(lambda a, b=0: a + b, 1, b=2) == 3


# ---------- classificação de exceções ----------


@pytest.mark.parametrize(
    "erro",
    [
        ConnectionError("x"),
        TimeoutError("x"),
        MiddlewareTimeoutError(timeout=1.0, elapsed=1.1),
        httpx.ConnectError("x"),
        httpx.ReadTimeout("x"),
    ],
    ids=[
        "ConnectionError",
        "TimeoutError",
        "MiddlewareTimeout",
        "ConnectError",
        "ReadTimeout",
    ],
)
def test_excecoes_transitorias_sao_retentadas(factory, sleeps, erro):
    func = Sequencia(erro, "ok")
    assert factory().call(func) == "ok"
    assert func.chamadas == 2
    assert len(sleeps) == 1


@pytest.mark.parametrize(
    "erro",
    [ValueError("x"), KeyError("x"), CircuitBreakerOpenException("c", 1.0)],
    ids=["ValueError", "KeyError", "CircuitOpen"],
)
def test_excecoes_nao_transitorias_falham_na_hora(factory, sleeps, erro):
    func = Sequencia(erro)
    with pytest.raises(type(erro)):
        factory().call(func)
    assert func.chamadas == 1
    assert sleeps == []


def test_excecoes_retentaveis_customizadas(factory):
    func = Sequencia(KeyError("x"), "ok")
    assert factory(retryable_exceptions=(KeyError,)).call(func) == "ok"
    assert func.chamadas == 2


def test_base_exception_nao_e_capturada(factory, sleeps):
    func = Sequencia(Abortar())
    with pytest.raises(Abortar):
        factory().call(func)
    assert func.chamadas == 1
    assert sleeps == []


# ---------- respostas HTTP ----------


@pytest.mark.parametrize("status", [502, 503, 504])
def test_status_transitorio_e_retentado(factory, sleeps, status):
    func = Sequencia(httpx.Response(status), httpx.Response(200))
    resposta = factory().call(func)
    assert resposta.status_code == 200
    assert func.chamadas == 2
    assert len(sleeps) == 1


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_erros_4xx_falham_na_primeira_sem_retentativa(factory, sleeps, status):
    func = Sequencia(httpx.Response(status))
    resposta = factory().call(func)
    assert resposta.status_code == status
    assert func.chamadas == 1
    assert sleeps == []


def test_status_500_nao_esta_na_lista_padrao(factory, sleeps):
    func = Sequencia(httpx.Response(500))
    assert factory().call(func).status_code == 500
    assert func.chamadas == 1
    assert sleeps == []


def test_status_transitorio_esgotado_devolve_a_ultima_resposta(factory, sleeps):
    func = Sequencia(httpx.Response(503))
    resposta = factory().call(func)
    assert resposta.status_code == 503
    assert func.chamadas == 3
    assert len(sleeps) == 2


def test_codigos_5xx_customizados(factory):
    func = Sequencia(httpx.Response(500), httpx.Response(200))
    resposta = factory(retryable_status_codes={500, 503}).call(func)
    assert resposta.status_code == 200
    assert func.chamadas == 2


def _erro_http(status):
    resposta = httpx.Response(status, request=httpx.Request("GET", "http://svc/x"))
    return httpx.HTTPStatusError("erro", request=resposta.request, response=resposta)


def test_http_status_error_503_e_retentado(factory):
    func = Sequencia(_erro_http(503), "ok")
    assert factory().call(func) == "ok"
    assert func.chamadas == 2


@pytest.mark.parametrize("status", [400, 404])
def test_http_status_error_4xx_nao_e_retentado(factory, sleeps, status):
    func = Sequencia(_erro_http(status))
    with pytest.raises(httpx.HTTPStatusError):
        factory().call(func)
    assert func.chamadas == 1
    assert sleeps == []


# ---------- logs ----------


def test_warning_a_cada_tentativa_intermediaria(factory, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with pytest.raises(ConnectionError):
        factory().call(Sequencia(ConnectionError("x")))

    avisos = eventos(caplog, logging.WARNING)
    assert len(avisos) == 2  # tentativas 1 e 2; a 3ª é a final
    assert [a["event"] for a in avisos] == ["retry_attempt", "retry_attempt"]
    assert [a["attempt"] for a in avisos] == [1, 2]
    assert all(a["max_attempts"] == 3 for a in avisos)
    assert [a["delay_s"] for a in avisos] == [0.5, 1.0]
    assert avisos[0]["delay_s"] < avisos[1]["delay_s"]
    assert avisos[0]["reason"] == "ConnectionError"


def test_esgotamento_loga_erro(factory, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with pytest.raises(ConnectionError):
        factory().call(Sequencia(ConnectionError("x")))
    erros = eventos(caplog, logging.ERROR)
    assert [e["event"] for e in erros] == ["retry_exhausted"]
    assert erros[0]["attempts"] == 3


def test_erro_nao_retentavel_nao_gera_warning(factory, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    factory().call(Sequencia(httpx.Response(404)))
    assert eventos(caplog, logging.WARNING) == []
    info = eventos(caplog, logging.INFO)
    assert info[0]["event"] == "retry_not_retryable"
    assert info[0]["reason"] == "status_404"


def test_sucesso_apos_retentativa_e_logado(factory, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    factory().call(Sequencia(ConnectionError("x"), "ok"))
    assert eventos(caplog, logging.INFO)[0] == {
        "event": "retry_succeeded",
        "operation": "Sequencia",
        "attempts": 2,
    }


def test_logs_nao_vazam_argumentos(factory, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    def chamar(segredo):
        raise ConnectionError("falha")

    with pytest.raises(ConnectionError):
        factory().call(chamar, "senha-super-secreta")
    assert "senha-super-secreta" not in caplog.text


# ---------- decorator ----------


def test_decorator_sync(factory, sleeps):
    func = Sequencia(ConnectionError("x"), "ok")

    @factory()
    def consultar(valor):
        return func() + valor

    assert consultar("!") == "ok!"
    assert consultar.__name__ == "consultar"
    assert len(sleeps) == 1


async def test_decorator_async(factory, sleeps):
    func = Sequencia(ConnectionError("x"), "ok")

    @factory()
    async def consultar():
        return await func.acall()

    assert await consultar() == "ok"
    assert consultar.__name__ == "consultar"
    assert len(sleeps) == 1


# ---------- call_async ----------


async def test_async_transitorio_seguido_de_sucesso(factory, sleeps):
    func = Sequencia(ConnectionError("x"), ConnectionError("x"), "ok")
    assert await factory().call_async(func.acall) == "ok"
    assert func.chamadas == 3
    assert sleeps == [0.5, 1.0]


async def test_async_esgota_e_relanca(factory, sleeps):
    func = Sequencia(ConnectionError("x"))
    with pytest.raises(ConnectionError):
        await factory().call_async(func.acall)
    assert func.chamadas == 3
    assert len(sleeps) == 2


async def test_async_erro_nao_retentavel_falha_na_hora(factory, sleeps):
    func = Sequencia(ValueError("x"))
    with pytest.raises(ValueError):
        await factory().call_async(func.acall)
    assert func.chamadas == 1
    assert sleeps == []


async def test_async_status_503_retentado_e_404_imediato(factory, sleeps):
    transitorio = Sequencia(httpx.Response(503), httpx.Response(200))
    assert (await factory().call_async(transitorio.acall)).status_code == 200
    assert transitorio.chamadas == 2

    sleeps.clear()
    nao_retentavel = Sequencia(httpx.Response(404))
    assert (await factory().call_async(nao_retentavel.acall)).status_code == 404
    assert nao_retentavel.chamadas == 1
    assert sleeps == []


async def test_async_cancelamento_nao_e_retentado(factory, sleeps):
    func = Sequencia(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await factory().call_async(func.acall)
    assert func.chamadas == 1
    assert sleeps == []


# ---------- critérios de aceite com o DistributedHttpClient real ----------


def _cliente(handler):
    return DistributedHttpClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_cliente_http_503_e_retentado_ate_dar_certo(factory, sleeps):
    respostas = [503, 503, 200]
    chamadas = []

    def handler(request):
        chamadas.append(1)
        return httpx.Response(respostas.pop(0))

    async with _cliente(handler) as cliente:
        resposta = await factory().call_async(
            cliente.get_catalog, "http://catalog/items"
        )

    assert resposta.status_code == 200
    assert len(chamadas) == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.parametrize("status", [400, 404])
async def test_cliente_http_4xx_falha_imediatamente(factory, sleeps, status):
    chamadas = []

    def handler(request):
        chamadas.append(1)
        return httpx.Response(status)

    async with _cliente(handler) as cliente:
        resposta = await factory().call_async(
            cliente.get_catalog, "http://catalog/items"
        )

    assert resposta.status_code == status
    assert len(chamadas) == 1  # sem retentativas inúteis
    assert sleeps == []


# ---------- composição com o circuit breaker ----------


def test_circuito_aberto_interrompe_as_retentativas(factory):
    breaker = CircuitBreaker("externo", failure_threshold=2, recovery_timeout=60)
    func = Sequencia(ConnectionError("x"))
    with pytest.raises(CircuitBreakerOpenException):
        factory(max_attempts=5).call(lambda: breaker.call(func))
    assert func.chamadas == 2  # o circuito abriu e a 3ª tentativa nem chegou ao serviço
