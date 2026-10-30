import json
import os
import pika

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")

def publish_event(event_data: dict):
    """
    Publica o evento de resultado do pagamento no RabbitMQ.
    """
    connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
    channel = connection.channel()

    # Usaremos um exchange do tipo 'topic' ou uma fila direta dependendo da arquitetura.
    # Para simplicidade, vamos publicar diretamente em uma fila de saída ou exchange.
    status_queue = event_data["status"] # ex: 'payment.approved' ou 'payment.rejected'
    
    channel.queue_declare(queue=status_queue, durable=True)

    channel.basic_publish(
        exchange='',
        routing_key=status_queue,
        body=json.dumps(event_data),
        properties=pika.BasicProperties(
            delivery_mode=2, # Torna a mensagem persistente
        )
    )
    print(f" [x] Evento publicado: {status_queue} -> {event_data['payment_id']}")
    connection.close()