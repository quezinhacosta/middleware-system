import importlib
import json
from pathlib import Path

import pytest

from middleware.transport.idempotency import IdempotencyManager, SqliteIdempotencyStore

PAYMENT_DIR = Path(__file__).resolve().parents[2] / "services" / "payment_service"
CORPO = json.dumps(
    {"event_id": "evt-1", "order_id": "ord-1", "payment_method": "pix"}
).encode()


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


@pytest.fixture
def payment(monkeypatch):
    pytest.importorskip("pika")
    monkeypatch.syspath_prepend(str(PAYMENT_DIR))  # o serviço importa `src.*`
    return importlib.import_module("src.consumer")


@pytest.fixture
def servico(payment, monkeypatch):
    estado = {"process": [], "publish": [], "falhas_process": [], "falhas_publish": []}

    def fake_process(evento):
        estado["process"].append(evento)
        if estado["falhas_process"]:
            raise estado["falhas_process"].pop(0)
        return {
            "order_id": evento["order_id"],
            "payment_id": "pay_1",
            "status": "payment.approved",
        }

    def fake_publish(resultado):
        if estado["falhas_publish"]:
            raise estado["falhas_publish"].pop(0)
        estado["publish"].append(resultado)

    monkeypatch.setattr(payment, "process_payment", fake_process)
    monkeypatch.setattr(payment, "publish_event", fake_publish)

    canal = FakeChannel()
    manager = IdempotencyManager(SqliteIdempotencyStore())
    consumidor = payment.PaymentConsumer(canal, manager, sleep=lambda s: None)
    return consumidor, canal, estado


def test_mesma_mensagem_tres_vezes_processa_pagamento_uma_vez(servico):
    consumidor, canal, estado = servico
    for tag in (1, 2, 3):
        consumidor.on_message(canal, FakeMethod(tag), None, CORPO)

    assert len(estado["process"]) == 1  # exatamente 1 processamento financeiro
    assert len(estado["publish"]) == 1
    assert canal.acks == [1, 2, 3]
    assert canal.nacks == []


def test_falha_transiente_no_pagamento_e_retentada(servico):
    consumidor, canal, estado = servico
    estado["falhas_process"] = [ConnectionError("banco fora")]
    consumidor.on_message(canal, FakeMethod(1), None, CORPO)
    assert len(estado["process"]) == 2
    assert len(estado["publish"]) == 1
    assert canal.acks == [1]


def test_falha_ao_publicar_resultado_e_retentada(servico, payment):
    consumidor, canal, estado = servico
    estado["falhas_publish"] = [payment.pika.exceptions.AMQPConnectionError()]
    consumidor.on_message(canal, FakeMethod(1), None, CORPO)
    assert len(estado["publish"]) == 1
    assert canal.acks == [1]
    assert canal.nacks == []


def test_evento_sem_event_id_vai_para_dlq_sem_cobrar(servico):
    consumidor, canal, estado = servico
    consumidor.on_message(canal, FakeMethod(9), None, b'{"order_id": "ord-1"}')
    assert estado["process"] == []
    assert canal.nacks == [(9, False)]
    assert canal.acks == []


def test_json_invalido_vai_para_dlq(servico):
    consumidor, canal, estado = servico
    consumidor.on_message(canal, FakeMethod(2), None, b"{quebrado")
    assert estado["process"] == []
    assert canal.nacks == [(2, False)]
