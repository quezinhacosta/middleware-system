import asyncio

import pytest

from middleware.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenException,
    CircuitState,
)


class Relogio:
    """Relógio manual para testes determinísticos (sem sleep)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def avancar(self, segundos):
        self.t += segundos


class Abortar(BaseException):
    """Simula KeyboardInterrupt/cancelamento (não é Exception)."""


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def cb(relogio):
    return CircuitBreaker(
        "teste", failure_threshold=3, recovery_timeout=10, clock=relogio
    )


def ok():
    return "ok"


def falha():
    raise ConnectionError("boom")


def abrir(cb):
    for _ in range(cb._failure_threshold):
        with pytest.raises(ConnectionError):
            cb.call(falha)


# ---------- validação de parâmetros ----------


def test_threshold_invalido():
    with pytest.raises(ValueError):
        CircuitBreaker("x", failure_threshold=0)


@pytest.mark.parametrize("valor", [0, -1])
def test_recovery_timeout_invalido(valor):
    with pytest.raises(ValueError):
        CircuitBreaker("x", recovery_timeout=valor)


# ---------- transições ----------


def test_inicia_fechado(cb):
    assert cb.state is CircuitState.CLOSED
    assert cb.failure_count == 0


def test_permanece_fechado_abaixo_do_limite(cb):
    for _ in range(2):
        with pytest.raises(ConnectionError):
            cb.call(falha)
    assert cb.state is CircuitState.CLOSED
    assert cb.failure_count == 2


def test_abre_ao_atingir_o_limite(cb):
    abrir(cb)
    assert cb.state is CircuitState.OPEN


def test_aberto_rejeita_sem_chamar_a_funcao(cb):
    abrir(cb)
    chamadas = []
    with pytest.raises(CircuitBreakerOpenException) as exc:
        cb.call(lambda: chamadas.append(1))
    assert chamadas == []
    assert exc.value.name == "teste"
    assert 0 < exc.value.retry_after <= 10


def test_retry_after_diminui_com_o_tempo(cb, relogio):
    abrir(cb)
    relogio.avancar(4)
    with pytest.raises(CircuitBreakerOpenException) as exc:
        cb.call(ok)
    assert exc.value.retry_after == pytest.approx(6)


def test_sucesso_zera_falhas_consecutivas(cb):
    for _ in range(2):
        with pytest.raises(ConnectionError):
            cb.call(falha)
    cb.call(ok)
    assert cb.failure_count == 0
    for _ in range(2):
        with pytest.raises(ConnectionError):
            cb.call(falha)
    assert cb.state is CircuitState.CLOSED


def test_apos_timeout_canario_com_sucesso_fecha(cb, relogio):
    abrir(cb)
    relogio.avancar(10)
    assert cb.call(ok) == "ok"
    assert cb.state is CircuitState.CLOSED
    assert cb.failure_count == 0


def test_canario_com_falha_reabre_e_reinicia_o_timeout(cb, relogio):
    abrir(cb)
    relogio.avancar(10)
    with pytest.raises(ConnectionError):
        cb.call(falha)
    assert cb.state is CircuitState.OPEN
    relogio.avancar(5)  # menos que o timeout contado a partir da reabertura
    with pytest.raises(CircuitBreakerOpenException):
        cb.call(ok)


def test_antes_do_timeout_continua_aberto(cb, relogio):
    abrir(cb)
    relogio.avancar(9.9)
    with pytest.raises(CircuitBreakerOpenException):
        cb.call(ok)
    assert cb.state is CircuitState.OPEN


def test_half_open_permite_apenas_um_canario(cb, relogio):
    abrir(cb)
    relogio.avancar(10)
    resultado = {}

    def canario():
        assert cb.state is CircuitState.HALF_OPEN
        try:
            cb.call(ok)  # segunda chamada enquanto o canário está em voo
        except CircuitBreakerOpenException as e:
            resultado["rejeitada"] = e
        return "canario"

    assert cb.call(canario) == "canario"
    assert "rejeitada" in resultado
    assert cb.state is CircuitState.CLOSED


# ---------- classificação de exceções ----------


def test_excecao_inesperada_nao_conta_como_falha_de_infra(relogio):
    cb = CircuitBreaker(
        "neg",
        failure_threshold=2,
        recovery_timeout=10,
        expected_exceptions=(ConnectionError,),
        clock=relogio,
    )

    def erro_de_negocio():
        raise ValueError("estoque insuficiente")

    for _ in range(5):
        with pytest.raises(ValueError):
            cb.call(erro_de_negocio)
    assert cb.state is CircuitState.CLOSED
    assert cb.failure_count == 0


def test_abort_no_canario_devolve_o_circuito_para_open(cb, relogio):
    abrir(cb)
    relogio.avancar(10)

    def cancela():
        raise Abortar()

    with pytest.raises(Abortar):
        cb.call(cancela)
    assert cb.state is CircuitState.OPEN
    # o próximo chamador vira o novo canário imediatamente
    assert cb.call(ok) == "ok"
    assert cb.state is CircuitState.CLOSED


def test_abort_com_circuito_fechado_nao_conta_falha(cb):
    def cancela():
        raise Abortar()

    with pytest.raises(Abortar):
        cb.call(cancela)
    assert cb.failure_count == 0
    assert cb.state is CircuitState.CLOSED


# ---------- decorador e async ----------


def test_decorador_sync(cb):
    @cb
    def soma(a, b):
        return a + b

    assert soma(1, 2) == 3
    assert soma.__name__ == "soma"


async def test_decorador_async(cb):
    @cb
    async def soma(a, b):
        return a + b

    assert await soma(1, 2) == 3


async def test_call_async_abre_e_rejeita(cb):
    async def falha_async():
        raise ConnectionError("boom")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await cb.call_async(falha_async)
    assert cb.state is CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenException):
        await cb.call_async(falha_async)


async def test_call_async_cancelamento_no_canario_volta_para_open(cb, relogio):
    abrir(cb)
    relogio.avancar(10)

    async def cancelada():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cb.call_async(cancelada)
    assert cb.state is CircuitState.OPEN
