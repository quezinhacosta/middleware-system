import os
import time

import pika
import pytest
from dotenv import load_dotenv

from middleware.transport.consumer import Topology, declare_topology
from tests.integrations.test_stack import stack  # noqa: F401

# Garante que as variáveis do .env sejam injetadas no processo do Pytest
load_dotenv()


@pytest.fixture
def rabbitmq_channel(stack):  # noqa: F811
    user = os.getenv("RABBITMQ_DEFAULT_USER", "guest")
    password = os.getenv("RABBITMQ_DEFAULT_PASS", "guest")
    host = os.getenv("RABBITMQ_HOST", "localhost")
    port = int(os.getenv("RABBITMQ_PORT", "5672"))

    credentials = pika.PlainCredentials(user, password)
    parameters = pika.ConnectionParameters(
        host=host, port=port, credentials=credentials
    )

    connection = None
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            connection = pika.BlockingConnection(parameters)
            break
        except pika.exceptions.ProbableAuthenticationError as e:
            # Falha imediatamente se a senha estiver incorreta (evita esperar 45s)
            pytest.fail(
                f"Erro de autenticação no RabbitMQ (usuário/senha do .env estão corretos?): {e}"
            )
        except (pika.exceptions.AMQPConnectionError, ConnectionRefusedError, OSError):
            time.sleep(2)

    if connection is None:
        pytest.fail(f"RabbitMQ não respondeu em {host}:{port} após 45s.")

    channel = connection.channel()
    yield channel
    try:
        connection.close()
    except Exception:
        pass


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

    # 4. Verifica se o RabbitMQ roteou para a DLQ
    dlq_method_frame, _, dlq_body = rabbitmq_channel.basic_get(
        queue=topology.dlq, auto_ack=True
    )

    assert dlq_method_frame is not None
    assert dlq_body == b"{json-invalido}"
