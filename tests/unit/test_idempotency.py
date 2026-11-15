import logging
import threading

import pytest

from middleware.transport.consumer import (
    EventConsumer,
    Topology,
    TransientError,
    UnrecoverableError,
)
from middleware.transport.idempotency import (
    AcquireResult,
    IdempotencyConfigError,
    IdempotencyManager,
    IdempotencyStoreError,
    IdempotentConsumerInterceptor,
    MessageInProgressError,
    SqliteIdempotencyStore,
    configure_default_manager,
    idempotent,
)


class Relogio:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def avancar(self, segundos):
        self.t += segundos


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def store(relogio):
    s = SqliteIdempotencyStore(":memory:", clock=relogio)
    yield s
    s.close()


@pytest.fixture
def manager(store):
    return IdempotencyManager(store)


@pytest.fixture(autouse=True)
def limpar_manager_padrao():
    configure_default_manager(None)
    yield
    configure_default_manager(None)


# ---------- store SQLite ----------


def test_ciclo_acquire_complete(store):
    assert store.acquire("k", 300) is AcquireResult.ACQUIRED
    assert store.acquire("k", 300) is AcquireResult.IN_PROGRESS
    store.complete("k", 86400)
    assert store.acquire("k", 300) is AcquireResult.DONE


def test_release_permite_nova_reserva(store):
    store.acquire("k", 300)
    store.release("k")
    assert store.acquire("k", 300) is AcquireResult.ACQUIRED


def test_release_nao_apaga_evento_concluido(store):
    store.acquire("k", 300)
    store.complete("k", 86400)
    store.release("k")
    assert store.acquire("k", 300) is AcquireResult.DONE


def test_lease_expira_e_permite_reprocessar(store, relogio):
    store.acquire("k", 300)
    relogio.avancar(299)
    assert store.acquire("k", 300) is AcquireResult.IN_PROGRESS
    relogio.avancar(2)
    assert store.acquire("k", 300) is AcquireResult.ACQUIRED


def test_chaves_independentes(store):
    assert store.acquire("a", 300) is AcquireResult.ACQUIRED
    assert store.acquire("b", 300) is AcquireResult.ACQUIRED


def test_registro_persiste_em_arquivo(tmp_path):
    caminho = str(tmp_path / "idem.db")
    primeiro = SqliteIdempotencyStore(caminho)
    primeiro.acquire("k", 300)
    primeiro.complete("k", 86400)
    primeiro.close()

    reaberto = SqliteIdempotencyStore(caminho)
    assert reaberto.acquire("k", 300) is AcquireResult.DONE
    reaberto.close()


def test_acquire_e_atomico_entre_threads(store):
    resultados = []
    barreira = threading.Barrier(20)

    def disputar():
        barreira.wait()
        resultados.append(store.acquire("disputada", 300))

    threads = [threading.Thread(target=disputar) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert resultados.count(AcquireResult.ACQUIRED) == 1
    assert resultados.count(AcquireResult.IN_PROGRESS) == 19


def test_acquire_e_atomico_entre_conexoes_diferentes(tmp_path):
    caminho = str(tmp_path / "idem.db")
    stores = [SqliteIdempotencyStore(caminho) for _ in range(8)]
    resultados = []
    barreira = threading.Barrier(len(stores))

    def disputar(s):
        barreira.wait()
        resultados.append(s.acquire("disputada", 300))

    threads = [threading.Thread(target=disputar, args=(s,)) for s in stores]
    [t.start() for t in threads]
    [t.join() for t in threads]
    [s.close() for s in stores]
    assert resultados.count(AcquireResult.ACQUIRED) == 1


# ---------- manager ----------


def test_run_once_executa_so_na_primeira_vez(manager):
    chamadas = []
    assert manager.run_once("k", lambda: chamadas.append(1)) is True
    assert manager.run_once("k", lambda: chamadas.append(1)) is False
    assert manager.run_once("k", lambda: chamadas.append(1)) is False
    assert chamadas == [1]


def test_falha_no_negocio_libera_para_retentativa(manager):
    def quebra():
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        manager.run_once("k", quebra)

    chamadas = []
    assert manager.run_once("k", lambda: chamadas.append(1)) is True
    assert chamadas == [1]


def test_entrega_em_andamento_lanca_erro_transiente(manager):
    manager.acquire("k")  # simula a 1ª entrega ainda processando
    chamadas = []
    with pytest.raises(MessageInProgressError):
        manager.run_once("k", lambda: chamadas.append(1))
    assert chamadas == []
    assert issubclass(MessageInProgressError, TransientError)


def test_ttl_de_24h_para_eventos_concluidos(manager, store, relogio):
    manager.run_once("k", lambda: None)
    relogio.avancar(86399)
    assert store.acquire("k", 300) is AcquireResult.DONE
    relogio.avancar(2)
    assert store.acquire("k", 300) is AcquireResult.ACQUIRED


def test_registro_indisponivel_falha_fechado():
    class StoreQuebrado:
        def acquire(self, key, lease_ttl):
            raise RuntimeError("registro fora do ar")

        def complete(self, key, ttl):
            pass

        def release(self, key):
            pass

    chamadas = []
    with pytest.raises(IdempotencyStoreError):
        IdempotencyManager(StoreQuebrado()).run_once("k", lambda: chamadas.append(1))
    assert chamadas == []
    assert issubclass(IdempotencyStoreError, TransientError)


def test_falha_ao_concluir_nao_propaga_nem_reexecuta(store, caplog):
    class StoreQueFalhaAoConcluir:
        def acquire(self, key, lease_ttl):
            return store.acquire(key, lease_ttl)

        def complete(self, key, ttl):
            raise RuntimeError("disco cheio")

        def release(self, key):
            store.release(key)

    caplog.set_level(logging.DEBUG, logger="middleware.transport.idempotency")
    chamadas = []
    mgr = IdempotencyManager(StoreQueFalhaAoConcluir())
    assert mgr.run_once("k", lambda: chamadas.append(1)) is True  # não levantou
    assert chamadas == [1]
    assert "idempotency_complete_failed" in caplog.text


@pytest.mark.parametrize("kw", [{"ttl_seconds": 0}, {"processing_ttl_seconds": -1}])
def test_parametros_invalidos(store, kw):
    with pytest.raises(ValueError):
        IdempotencyManager(store, **kw)


# ---------- interceptor ----------


def test_interceptor_executa_uma_vez_e_loga_duplicidade(manager, caplog):
    caplog.set_level(logging.DEBUG, logger="middleware.transport.idempotency")
    chamadas = []
    interceptor = IdempotentConsumerInterceptor(
        lambda env: chamadas.append(env), manager, namespace="payments"
    )
    for _ in range(3):
        interceptor({"event_id": "evt-1", "amount": 10})

    assert len(chamadas) == 1
    assert caplog.text.count("MESSAGE_ALREADY_PROCESSED") == 2
    avisos = [
        r for r in caplog.records if "MESSAGE_ALREADY_PROCESSED" in r.getMessage()
    ]
    assert all(r.levelno == logging.WARNING for r in avisos)
    assert "evt-1" in avisos[0].getMessage()


def test_namespaces_diferentes_nao_se_bloqueiam(manager):
    pagamento, notificacao = [], []
    IdempotentConsumerInterceptor(pagamento.append, manager, namespace="payments")(
        {"event_id": "e1"}
    )
    IdempotentConsumerInterceptor(
        notificacao.append, manager, namespace="notifications"
    )({"event_id": "e1"})
    assert len(pagamento) == 1 and len(notificacao) == 1


def test_event_ids_diferentes_executam_ambos(manager):
    chamadas = []
    interceptor = IdempotentConsumerInterceptor(chamadas.append, manager)
    interceptor({"event_id": "a"})
    interceptor({"event_id": "b"})
    assert len(chamadas) == 2


def test_event_id_inteiro_e_aceito(manager):
    chamadas = []
    interceptor = IdempotentConsumerInterceptor(chamadas.append, manager)
    interceptor({"event_id": 123})
    interceptor({"event_id": 123})
    assert len(chamadas) == 1


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"event_id": None},
        {"event_id": ""},
        {"event_id": "   "},
        {"event_id": ["x"]},
        {"event_id": True},
    ],
    ids=["ausente", "none", "vazio", "espacos", "lista", "bool"],
)
def test_event_id_ausente_ou_invalido_nao_processa(manager, envelope):
    chamadas = []
    with pytest.raises(UnrecoverableError):
        IdempotentConsumerInterceptor(chamadas.append, manager)(envelope)
    assert chamadas == []


def test_chave_customizada(manager):
    chamadas = []
    interceptor = IdempotentConsumerInterceptor(
        chamadas.append, manager, key="message_id"
    )
    interceptor({"message_id": "m1"})
    interceptor({"message_id": "m1"})
    assert len(chamadas) == 1


# ---------- decorator ----------


def test_decorator_em_funcao_com_manager_explicito(manager):
    chamadas = []

    @idempotent(key="event_id", manager=manager)
    def handler(envelope):
        chamadas.append(envelope)

    handler({"event_id": "x"})
    handler({"event_id": "x"})
    assert len(chamadas) == 1
    assert handler.__name__ == "handler"


def test_decorator_usa_manager_padrao(manager):
    configure_default_manager(manager)
    chamadas = []

    @idempotent(key="event_id")
    def handler(envelope):
        chamadas.append(envelope)

    handler({"event_id": "x"})
    handler({"event_id": "x"})
    assert len(chamadas) == 1


def test_decorator_sem_manager_falha_com_erro_claro():
    @idempotent(key="event_id")
    def handler(envelope):
        pass

    with pytest.raises(IdempotencyConfigError):
        handler({"event_id": "x"})


def test_decorator_exige_envelope():
    @idempotent(key="event_id")
    def handler(valor):
        pass

    configure_default_manager(IdempotencyManager(SqliteIdempotencyStore()))
    with pytest.raises(TypeError):
        handler("nao-e-dict")


# ---------- integração com o EventConsumer (critério de aceite) ----------


class FakeMethod:
    def __init__(self, delivery_tag):
        self.delivery_tag = delivery_tag


class FakeChannel:
    def __init__(self):
        self.acks = []
        self.nacks = []

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue=True):
        self.nacks.append((delivery_tag, requeue))


class PaymentConsumer(EventConsumer):
    def __init__(self, channel, topology, manager, **kw):
        super().__init__(channel, topology, **kw)
        self.idempotency_manager = manager
        self.cobrancas = []
        self.comportamentos = []

    @idempotent(key="event_id", namespace="payments")
    def handle_message(self, envelope):
        if self.comportamentos:
            comportamento = self.comportamentos.pop(0)
            if comportamento is not None:
                raise comportamento
        self.cobrancas.append(envelope["amount"])


@pytest.fixture
def ch():
    return FakeChannel()


@pytest.fixture
def consumidor(ch, manager):
    topology = Topology(
        exchange="payments", queue="payments.requested", routing_key="payment.requested"
    )
    return PaymentConsumer(ch, topology, manager, sleep=lambda s: None)


CORPO = b'{"event_id": "evt-1", "amount": 100}'


def test_mesma_mensagem_tres_vezes_processa_uma_vez(consumidor, ch, caplog):
    caplog.set_level(logging.DEBUG)
    for tag in (1, 2, 3):
        consumidor.on_message(ch, FakeMethod(tag), None, CORPO)

    assert consumidor.cobrancas == [100]  # exatamente 1 processamento financeiro
    assert ch.acks == [1, 2, 3]  # as 3 entregas são confirmadas
    assert ch.nacks == []
    assert caplog.text.count("MESSAGE_ALREADY_PROCESSED") == 2


def test_falha_transiente_e_reprocessada_sem_duplicar(consumidor, ch):
    consumidor.comportamentos = [TransientError("gateway fora"), None]
    consumidor.on_message(ch, FakeMethod(1), None, CORPO)
    assert consumidor.cobrancas == [100]
    assert ch.acks == [1]
    assert ch.nacks == []


def test_entrega_duplicada_durante_processamento_espera_e_confirma(ch, manager):
    manager.acquire("payments:evt-1")  # 1ª entrega ainda processando
    topology = Topology(
        exchange="payments", queue="payments.requested", routing_key="payment.requested"
    )
    # ao esperar o backoff, a 1ª entrega termina
    consumidor = PaymentConsumer(
        ch, topology, manager, sleep=lambda s: manager.complete("payments:evt-1")
    )
    consumidor.on_message(ch, FakeMethod(7), None, CORPO)
    assert consumidor.cobrancas == []  # não reprocessou
    assert ch.acks == [7]
    assert ch.nacks == []


def test_mensagem_sem_event_id_vai_para_dlq(consumidor, ch):
    consumidor.on_message(ch, FakeMethod(4), None, b'{"amount": 100}')
    assert consumidor.cobrancas == []
    assert ch.nacks == [(4, False)]
    assert ch.acks == []


def test_registro_fora_do_ar_nao_cobra_e_vai_para_dlq(ch):
    class StoreQuebrado:
        def acquire(self, key, lease_ttl):
            raise RuntimeError("fora do ar")

        def complete(self, key, ttl):
            pass

        def release(self, key):
            pass

    topology = Topology(
        exchange="payments", queue="payments.requested", routing_key="payment.requested"
    )
    consumidor = PaymentConsumer(
        ch,
        topology,
        IdempotencyManager(StoreQuebrado()),
        sleep=lambda s: None,
        max_retries=2,
    )
    consumidor.on_message(ch, FakeMethod(5), None, CORPO)
    assert consumidor.cobrancas == []
    assert ch.nacks == [(5, False)]
