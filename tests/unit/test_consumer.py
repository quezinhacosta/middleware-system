import logging

import pytest

from middleware.resilience.circuit_breaker import CircuitBreakerOpenException
from middleware.resilience.exceptions import MiddlewareTimeoutError
from middleware.transport.consumer import (
    EventConsumer,
    Topology,
    TransientError,
    UnrecoverableError,
    declare_topology,
)


class FakeMethod:
    def __init__(self, delivery_tag):
        self.delivery_tag = delivery_tag


class FakeChannel:
    """Canal falso: registra as chamadas, sem rede."""

    def __init__(self):
        self.calls = []
        self.acks = []
        self.nacks = []
        self.consumer_kwargs = None
        self.consuming = False
        self.stopped = False

    def exchange_declare(self, **kw):
        self.calls.append(("exchange_declare", kw))

    def queue_declare(self, **kw):
        self.calls.append(("queue_declare", kw))

    def queue_bind(self, **kw):
        self.calls.append(("queue_bind", kw))

    def basic_qos(self, **kw):
        self.calls.append(("basic_qos", kw))

    def basic_consume(self, **kw):
        self.calls.append(("basic_consume", kw))
        self.consumer_kwargs = kw

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue=True):
        self.nacks.append((delivery_tag, requeue))

    def start_consuming(self):
        self.consuming = True

    def stop_consuming(self):
        self.stopped = True


class Recorder(EventConsumer):
    """Handler de teste: cada item de `behaviors` é uma exceção a lançar ou None (sucesso).
    Esgotada a lista, o handler passa a ter sucesso."""

    def __init__(self, channel, topology, behaviors=None, **kw):
        super().__init__(channel, topology, **kw)
        self.calls = []
        self.behaviors = list(behaviors or [])

    def handle_message(self, envelope):
        self.calls.append(envelope)
        if self.behaviors:
            comportamento = self.behaviors.pop(0)
            if comportamento is not None:
                raise comportamento


@pytest.fixture
def topology():
    return Topology(
        exchange="orders", queue="orders.created", routing_key="order.created"
    )


@pytest.fixture
def ch():
    return FakeChannel()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def make(ch, topology, sleeps):
    def _make(behaviors=None, cls=Recorder, **kw):
        kw.setdefault("sleep", sleeps.append)
        kw.setdefault("backoff_base", 0.5)
        return cls(ch, topology, behaviors=behaviors, **kw)

    return _make


def enviar(consumer, ch, body, tag=1):
    consumer.on_message(ch, FakeMethod(tag), None, body)


# ---------- topologia ----------


def test_nomes_da_topologia(topology):
    assert topology.dlx == "orders.dlx"
    assert topology.dlq == "orders.created.dlq"
    assert topology.dlq_routing_key == "orders.created.dlq"


def test_declare_topology_cria_dlx_dlq_e_fila_principal(ch, topology):
    declare_topology(ch, topology)

    exchanges = [kw for nome, kw in ch.calls if nome == "exchange_declare"]
    assert [e["exchange"] for e in exchanges] == ["orders.dlx", "orders"]
    assert all(e["durable"] for e in exchanges)

    filas = [kw for nome, kw in ch.calls if nome == "queue_declare"]
    assert [f["queue"] for f in filas] == ["orders.created.dlq", "orders.created"]
    assert all(f["durable"] for f in filas)
    assert filas[1]["arguments"] == {
        "x-dead-letter-exchange": "orders.dlx",
        "x-dead-letter-routing-key": "orders.created.dlq",
    }

    binds = [kw for nome, kw in ch.calls if nome == "queue_bind"]
    assert {
        "queue": "orders.created.dlq",
        "exchange": "orders.dlx",
        "routing_key": "orders.created.dlq",
    } in binds
    assert {
        "queue": "orders.created",
        "exchange": "orders",
        "routing_key": "order.created",
    } in binds


def test_setup_configura_qos_e_consumo_com_ack_manual(make, ch):
    consumer = make(prefetch_count=7)
    consumer.setup()
    assert ("basic_qos", {"prefetch_count": 7}) in ch.calls
    assert ch.consumer_kwargs["queue"] == "orders.created"
    assert ch.consumer_kwargs["auto_ack"] is False
    assert ch.consumer_kwargs["on_message_callback"] == consumer.on_message


def test_start_e_stop(make, ch):
    consumer = make()
    consumer.start()
    assert ch.consuming is True
    consumer.stop()
    assert ch.stopped is True


# ---------- sucesso ----------


def test_mensagem_valida_recebe_ack(make, ch, sleeps):
    consumer = make()
    enviar(consumer, ch, b'{"order_id": 1}', tag=42)
    assert consumer.calls == [{"order_id": 1}]
    assert ch.acks == [42]
    assert ch.nacks == []
    assert sleeps == []


# ---------- mensagens inválidas (poison messages) ----------


@pytest.mark.parametrize(
    "corpo",
    [b"{quebrado", b"", b"\x80abc", b"[1, 2]", b'"texto"', b"123", b"null"],
    ids=[
        "json-quebrado",
        "vazio",
        "utf8-invalido",
        "lista",
        "string",
        "numero",
        "null",
    ],
)
def test_corpo_invalido_vai_para_dlq_sem_chamar_handler(make, ch, sleeps, corpo):
    consumer = make()
    enviar(consumer, ch, corpo, tag=5)
    assert consumer.calls == []
    assert ch.nacks == [(5, False)]  # requeue=False -> DLQ
    assert ch.acks == []
    assert sleeps == []


def test_consumidor_continua_saudavel_apos_poison_message(make, ch):
    consumer = make()
    enviar(consumer, ch, b"{quebrado", tag=1)
    enviar(consumer, ch, b'{"id": 1}', tag=2)
    assert ch.nacks == [(1, False)]
    assert ch.acks == [2]
    assert consumer.calls == [{"id": 1}]


# ---------- erros irrecuperáveis ----------


@pytest.mark.parametrize(
    "erro",
    [UnrecoverableError("x"), ValueError("x"), KeyError("campo"), RuntimeError("x")],
    ids=["Unrecoverable", "ValueError", "KeyError", "RuntimeError"],
)
def test_erro_irrecuperavel_vai_direto_para_dlq(make, ch, sleeps, erro):
    consumer = make(behaviors=[erro])
    enviar(consumer, ch, b'{"id": 1}', tag=9)
    assert len(consumer.calls) == 1  # sem retentativas
    assert ch.nacks == [(9, False)]
    assert ch.acks == []
    assert sleeps == []


# ---------- erros transientes ----------


def test_transiente_seguido_de_sucesso_recebe_ack_com_backoff(make, ch, sleeps):
    consumer = make(behaviors=[TransientError("a"), TransientError("b"), None])
    enviar(consumer, ch, b'{"id": 1}', tag=3)
    assert len(consumer.calls) == 3
    assert ch.acks == [3]
    assert ch.nacks == []
    assert sleeps == [0.5, 1.0]


def test_retentativas_esgotadas_vao_para_dlq(make, ch, sleeps):
    consumer = make(behaviors=[TransientError("x")] * 10, max_retries=2)
    enviar(consumer, ch, b'{"id": 1}', tag=4)
    assert len(consumer.calls) == 3  # 1 tentativa + 2 retentativas
    assert ch.nacks == [(4, False)]
    assert ch.acks == []
    assert len(sleeps) == 2


def test_sem_retentativas_falha_transiente_vai_direto_para_dlq(make, ch, sleeps):
    consumer = make(behaviors=[TransientError("x")], max_retries=0)
    enviar(consumer, ch, b'{"id": 1}')
    assert len(consumer.calls) == 1
    assert ch.nacks == [(1, False)]
    assert sleeps == []


def test_backoff_exponencial_respeita_o_teto(make, ch, sleeps):
    consumer = make(
        behaviors=[TransientError("x")] * 10,
        max_retries=5,
        backoff_base=1.0,
        backoff_max=3.0,
    )
    enviar(consumer, ch, b'{"id": 1}')
    assert len(consumer.calls) == 6
    assert sleeps == [1.0, 2.0, 3.0, 3.0, 3.0]


@pytest.mark.parametrize(
    "erro",
    [
        TransientError("x"),
        ConnectionError("x"),
        TimeoutError("x"),
        MiddlewareTimeoutError(timeout=1.0, elapsed=1.1),
        CircuitBreakerOpenException("catalog", 2.0),
    ],
    ids=[
        "Transient",
        "ConnectionError",
        "TimeoutError",
        "MiddlewareTimeout",
        "CircuitOpen",
    ],
)
def test_erros_transientes_padrao_sao_retentados(make, ch, sleeps, erro):
    consumer = make(behaviors=[erro, None])
    enviar(consumer, ch, b'{"id": 1}')
    assert len(consumer.calls) == 2
    assert ch.acks == [1]
    assert len(sleeps) == 1


def test_subclasse_pode_redefinir_erros_transientes(make, ch, sleeps):
    class Custom(Recorder):
        transient_exceptions = (KeyError,)

    retentado = make(behaviors=[KeyError("x"), None], cls=Custom)
    enviar(retentado, ch, b'{"id": 1}', tag=1)
    assert ch.acks == [1]

    descartado = make(behaviors=[ConnectionError("x")], cls=Custom)
    enviar(descartado, ch, b'{"id": 2}', tag=2)
    assert ch.nacks == [(2, False)]  # ConnectionError deixou de ser transiente


# ---------- logs ----------


def test_dlq_gera_log_sem_vazar_o_corpo(make, ch, caplog):
    caplog.set_level(logging.DEBUG, logger="middleware.transport.consumer")
    consumer = make()
    enviar(consumer, ch, b'{"senha": "senha-super-secreta"', tag=8)
    assert "consumer_dead_letter" in caplog.text
    assert "invalid_message" in caplog.text
    assert "senha-super-secreta" not in caplog.text


# ---------- validação ----------


def test_handle_message_e_abstrato(ch, topology):
    with pytest.raises(TypeError):
        EventConsumer(ch, topology)


@pytest.mark.parametrize(
    "kw",
    [
        {"max_retries": -1},
        {"backoff_base": -1},
        {"backoff_max": -1},
        {"prefetch_count": 0},
    ],
)
def test_parametros_invalidos(make, kw):
    with pytest.raises(ValueError):
        make(**kw)
