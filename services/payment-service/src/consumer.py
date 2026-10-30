import json
import os
import time

import pika
from src.processor import process_payment
from src.publisher import publish_event

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
QUEUE_NAME = "order.created"


def callback(ch, method, properties, body):
    """
    Função executada toda vez que uma mensagem chega na fila.
    """
    try:
        event_data = json.loads(body)
        print(f" [x] Mensagem recebida para o pedido: {event_data.get('order_id')}")

        # 1. Processa o pagamento (simulação com latência e tipos: PIX, cartão, boleto)
        result = process_payment(event_data)

        # 2. Publica o evento de saída (payment.approved ou payment.rejected)
        publish_event(result)

        # 3. ACK (Confirmação): Apenas reconhece a mensagem após sucesso garantido
        ch.basic_ack(delivery_tag=method.delivery_tag)
        print(
            f" [x] Mensagem processada e ACK enviado para o pedido: {result['order_id']}"
        )

    except Exception as e:
        print(f" [!] Erro ao processar mensagem: {e}")
        # Em caso de erro crítico, podemos rejeitar e mandar para uma Dead Letter Queue (DLQ)
        # ou reenfileirar (requeue=False para evitar loop infinito nesta fase simples)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def start_consuming():
    """
    Conecta ao RabbitMQ e inicia o loop de consumo em background.
    """
    while True:
        try:
            print(f" [*] Tentando conectar ao RabbitMQ em {RABBITMQ_HOST}...")
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=RABBITMQ_HOST)
            )
            channel = connection.channel()

            # Declara a fila para garantir que ela existe
            channel.queue_declare(queue=QUEUE_NAME, durable=True)

            # Garante que o worker só pega uma mensagem por vez (fair dispatch)
            channel.basic_qos(prefetch_count=1)

            # Define qual função vai tratar as mensagens da fila
            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=callback)

            print(" [*] Aguardando mensagens. Para sair pressione CTRL+C")
            channel.start_consuming()

        except pika.exceptions.AMQPConnectionError:
            print(
                " [!] Falha na conexão com o RabbitMQ. Tentando novamente em 5 segundos..."
            )
            time.sleep(5)
        except KeyboardInterrupt:
            print(" [x] Consumidor interrompido pelo usuário.")
            break
