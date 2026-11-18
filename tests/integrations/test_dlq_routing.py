import os
import subprocess
import time

import pika
import pytest
from dotenv import load_dotenv

from middleware.transport.consumer import Topology, declare_topology
from tests.integrations.helpers import compose

load_dotenv()


def _container_em_execucao(nome: str) -> bool:
    res = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", nome],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0 and res.stdout.strip() == "true"


def _obter_credenciais_broker() -> tuple[str, str]:
    res = subprocess.run(
        [
            "docker",
            "exec",
            "message-broker",
            "sh",
            "-c",
            'printf "%s\\n%s" "${RABBITMQ_DEFAULT_USER:-guest}" "${RABBITMQ_DEFAULT_PASS:-guest}"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0:
        linhas = res.stdout.splitlines()
        if len(linhas) >= 2:
            user = linhas[0].strip() or "guest"
            password = linhas[1].strip() or "guest"
            return user, password

    return (
        os.getenv("RABBITMQ_DEFAULT_USER", "guest"),
        os.getenv("RABBITMQ_DEFAULT_PASS", "guest"),
    )


@pytest.fixture
def rabbitmq_channel():
    ja_rodava = _container_em_execucao("message-broker")

    if not ja_rodava:
        # Usa os arquivos de override e as credenciais de teste do helper,
        # e não os volumes de desenvolvimento.
        resultado = compose("up", "-d", "message-broker")
        if resultado.returncode != 0:
            pytest.fail(f"docker compose up falhou:\n{resultado.stderr[-1500:]}")

    host = os.getenv("RABBITMQ_HOST", "localhost")
    port = int(os.getenv("RABBITMQ_PORT", "5672"))

    connection = None
    ultimo_erro: Exception | None = None
    deadline = time.time() + 45

    try:
        while time.time() < deadline:
            user, password = _obter_credenciais_broker()
            credentials = pika.PlainCredentials(user, password)
            parameters = pika.ConnectionParameters(
                host=host, port=port, credentials=credentials
            )
            try:
                connection = pika.BlockingConnection(parameters)
                break
            except (
                pika.exceptions.AMQPConnectionError,
                ConnectionRefusedError,
                OSError,
            ) as exc:
                ultimo_erro = exc
                time.sleep(2)

        if connection is None:
            pytest.fail(
                f"RabbitMQ não respondeu em {host}:{port} após 45s. Último erro: {ultimo_erro}"
            )

        channel = connection.channel()
        yield channel
    finally:
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except Exception:
                pass
        if not ja_rodava:
            # Usa os mesmos arquivos de override do conftest: sem eles, o `down -v`
            # removeria também os volumes de desenvolvimento.
            compose("down", "-v")


@pytest.mark.integration
def test_rabbitmq_roteia_nack_para_dlq(rabbitmq_channel):
    topology = Topology(
        exchange="test.integration.ex",
        queue="test.integration.q",
        routing_key="test.key",
    )

    # 1. Cria a estrutura no broker
    declare_topology(rabbitmq_channel, topology)
    rabbitmq_channel.queue_purge(topology.queue)
    rabbitmq_channel.queue_purge(topology.dlq)

    # 2. Publica uma poison message
    rabbitmq_channel.basic_publish(
        exchange=topology.exchange,
        routing_key=topology.routing_key,
        body=b"{json-invalido}",
    )

    # 3. Consome da fila principal e dá NACK sem requeue
    method_frame, _, body = rabbitmq_channel.basic_get(queue=topology.queue)
    assert method_frame is not None
    assert body == b"{json-invalido}"

    rabbitmq_channel.basic_nack(delivery_tag=method_frame.delivery_tag, requeue=False)

    # 4. Verifica se o RabbitMQ encaminhou para a DLQ (aguardando o roteamento assíncrono)
    dlq_method_frame = None
    dlq_body = None

    limite = time.time() + 5.0  # Aguarda no máximo 5 segundos
    while time.time() < limite:
        dlq_method_frame, _, dlq_body = rabbitmq_channel.basic_get(
            queue=topology.dlq, auto_ack=True
        )
        if dlq_method_frame is not None:
            break  # Achou a mensagem, sai do loop imediatamente
        time.sleep(0.1)  # Espera 100ms e tenta de novo

    assert dlq_method_frame is not None, "A mensagem não chegou na DLQ a tempo"
    assert dlq_body == b"{json-invalido}"
