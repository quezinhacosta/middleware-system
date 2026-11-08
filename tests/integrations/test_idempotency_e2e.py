import time
from uuid import uuid4

import pytest

from tests.integrations.helpers import (
    esperar,
    fila_existe,
    logs_do_container,
    mensagens_na_fila,
    publicar_evento,
    publicar_na_fila,
)

pytestmark = pytest.mark.integration

FILA_ENTRADA = "order.created"
FILA_DLQ = "order.created.dlq"
FILAS_RESULTADO = ("payment.approved", "payment.rejected")
# As estatísticas do RabbitMQ atualizam a cada ~5s: janela para um duplicado aparecer.
JANELA_DE_ESTABILIZACAO = 10


def resultados():
    return sum(mensagens_na_fila(f) for f in FILAS_RESULTADO)


def novo_evento():
    return {
        "event_id": uuid4().hex,
        "order_id": f"ord-{uuid4().hex[:8]}",
        "payment_method": "pix",
    }


@pytest.fixture(scope="module")
def tres_entregas(stack):
    """Injeta a MESMA mensagem 3 vezes e espera o consumidor terminar."""
    esperar(
        lambda: fila_existe(FILA_ENTRADA),
        90,
        f"a fila {FILA_ENTRADA} ser declarada pelo Payment Service",
    )
    evento = novo_evento()
    for _ in range(3):
        assert publicar_evento(FILA_ENTRADA, evento) is True
    esperar(lambda: resultados() >= 1, 60, "o pagamento ser processado")
    esperar(
        lambda: mensagens_na_fila(FILA_ENTRADA) == 0, 60, "a fila de entrada esvaziar"
    )
    time.sleep(JANELA_DE_ESTABILIZACAO)
    return evento


def test_tres_entregas_geram_um_unico_processamento(tres_entregas):
    assert resultados() == 1


def test_entregas_duplicadas_sao_confirmadas_e_nao_vao_para_dlq(tres_entregas):
    assert mensagens_na_fila(FILA_ENTRADA) == 0
    assert mensagens_na_fila(FILA_DLQ) == 0


def test_duplicidade_e_logada_duas_vezes(tres_entregas):
    linhas = logs_do_container("payment-service").splitlines()
    avisos = [
        linha
        for linha in linhas
        if "MESSAGE_ALREADY_PROCESSED" in linha and tres_entregas["event_id"] in linha
    ]
    assert len(avisos) == 2


def test_mensagem_invalida_vai_para_dlq_e_consumidor_continua_saudavel(tres_entregas):
    # depende dos testes acima (DLQ vazia e 1 resultado); mantenha a ordem
    assert publicar_na_fila(FILA_ENTRADA, "{quebrado") is True
    esperar(
        lambda: mensagens_na_fila(FILA_DLQ) == 1,
        40,
        "a mensagem inválida chegar na DLQ",
    )

    # o consumidor segue vivo: um evento válido novo ainda é processado
    assert publicar_evento(FILA_ENTRADA, novo_evento()) is True
    esperar(lambda: resultados() == 2, 60, "o evento válido seguinte ser processado")
