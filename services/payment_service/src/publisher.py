import json
import os

import pika

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "message-broker")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", 5672))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")

credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
parameters = pika.ConnectionParameters(
    host=RABBITMQ_HOST, port=RABBITMQ_PORT, credentials=credentials
)


def publish_event(event_data: dict):
    """
    Publica o evento de resultado do pagamento no RabbitMQ.
    """
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    # Usaremos um exchange do tipo 'topic' ou uma fila direta dependendo da arquitetura.
    # Para simplicidade, vamos publicar diretamente em uma fila de saída ou exchange.
    status_queue = event_data["status"]  # ex: 'payment.approved' ou 'payment.rejected'

    channel.queue_declare(queue=status_queue, durable=True)

    channel.basic_publish(
        exchange="",
        routing_key=status_queue,
        body=json.dumps(event_data),
        properties=pika.BasicProperties(
            delivery_mode=2,  # Torna a mensagem persistente
        ),
    )
    print(f" [x] Evento publicado: {status_queue} -> {event_data['payment_id']}")
    connection.close()
