import os

import pika
import pytest

from middleware.transport.consumer import Topology, declare_topology


@pytest.fixture
def rabbitmq_channel():
    user = os.getenv("RABBITMQ_DEFAULT_USER", "guest")
    password = os.getenv("RABBITMQ_DEFAULT_PASS", "guest")
    credentials = pika.PlainCredentials(user, password)

    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host="localhost", port=5672, credentials=credentials)
    )
    channel = connection.channel()
    yield channel
    connection.close()


def test_rabbitmq_roteia_nack_para_dlq(rabbitmq_channel):
    topology = Topology(
        exchange="test.integration.ex",
        queue="test.integration.q",
        routing_key="test.key",
    )

    # 1. Cria a estrutura real no broker
    declare_topology(rabbitmq_channel, topology)
    rabbitmq_channel.queue_purge(topology.queue)
    rabbitmq_channel.queue_purge(topology.dlq)

    # 2. Publica uma "poison message"
    rabbitmq_channel.basic_publish(
        exchange=topology.exchange,
        routing_key=topology.routing_key,
        body=b"{json-invalido}",
    )

    # 3. Consome a mensagem da fila principal e dá NACK
    method_frame, _, body = rabbitmq_channel.basic_get(queue=topology.queue)
    assert body == b"{json-invalido}"

    rabbitmq_channel.basic_nack(delivery_tag=method_frame.delivery_tag, requeue=False)

    # 4. Verifica se a mensagem foi roteada automaticamente para a DLQ
    dlq_method_frame, _, dlq_body = rabbitmq_channel.basic_get(
        queue=topology.dlq, auto_ack=True
    )

    assert dlq_body == b"{json-invalido}"
    assert dlq_method_frame is not None
