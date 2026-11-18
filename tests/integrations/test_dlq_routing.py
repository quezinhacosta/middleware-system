import os
import subprocess
import time
from collections.abc import Iterator

import pika
import pika.exceptions
import pytest
from dotenv import load_dotenv
from pika.adapters.blocking_connection import BlockingChannel

from middleware.transport.consumer import Topology, declare_topology

load_dotenv()


def _docker_disponivel() -> bool:
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, check=False)
    except FileNotFoundError:
        return False
    return res.returncode == 0


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
def rabbitmq_channel() -> Iterator[BlockingChannel]:
    if not _docker_disponivel():
        pytest.skip("Docker indisponível: teste de integração pulado")

    ja_rodava = _container_em_execucao("message-broker")
    env = {
        **os.environ,
        "POSTGRES_USER": os.environ.get("POSTGRES_USER", "postgres"),
        "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
        "RABBITMQ_DEFAULT_USER": os.environ.get("RABBITMQ_DEFAULT_USER", "guest"),
        "RABBITMQ_DEFAULT_PASS": os.environ.get("RABBITMQ_DEFAULT_PASS", "guest"),
        # O compose valida todas as variáveis obrigatórias, mesmo ao subir só o broker
        "GRAFANA_ADMIN_PASSWORD": os.environ.get(
            "GRAFANA_ADMIN_PASSWORD", "teste_grafana_nao_e_de_producao"
        ),
        "SERVICE_TOKEN_SECRET": os.environ.get(
            "SERVICE_TOKEN_SECRET", "test-secret-key"
        ),
    }

    if not ja_rodava:
        res = subprocess.run(
            ["docker", "compose", "up", "-d", "message-broker"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            pytest.fail(
                "docker compose up -d message-broker falhou "
                f"(código {res.returncode}):\n{res.stderr or res.stdout}"
            )

    host = os.getenv("RABBITMQ_TEST_HOST", "localhost")
    port = int(os.getenv("RABBITMQ_TEST_PORT", "5672"))

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
            # Remove só o broker (e seus volumes anônimos). `compose down -v`
            # derrubaria todos os serviços e apagaria os volumes do projeto.
            subprocess.run(
                ["docker", "compose", "rm", "-sfv", "message-broker"],
                env=env,
                check=False,
            )


@pytest.mark.integration
def test_rabbitmq_roteia_nack_para_dlq(rabbitmq_channel: BlockingChannel) -> None:
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
    assert method_frame.delivery_tag is not None
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
