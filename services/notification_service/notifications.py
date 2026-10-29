"""Regras de notificação: transforma eventos de pagamento e pedido em avisos."""

import logging
from collections import deque
from typing import Any

from middleware.observability.correlation import correlation_id_var
from middleware.transport.publisher import EventEnvelope

logger = logging.getLogger(__name__)

HISTORY_SIZE = 100

# Eventos que geram notificação ao cliente, com o texto de cada um
MESSAGES: dict[str, str] = {
    "payment.approved": "Pagamento do pedido {order_id} aprovado",
    "payment.rejected": "Pagamento do pedido {order_id} recusado",
    "order.cancelled": "Pedido {order_id} cancelado",
}

# Últimas notificações enviadas, para conferência via GET /notifications
history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SIZE)


def build_notification(envelope: EventEnvelope) -> dict[str, Any]:
    """Monta a notificação simulada. Levanta KeyError para evento desconhecido."""
    template = MESSAGES[envelope.event_name]
    order_id = envelope.data.get("order_id", "desconhecido")
    return {
        "event_name": envelope.event_name,
        "correlation_id": envelope.correlation_id,
        "to": envelope.data.get("customer_email", "cliente-desconhecido"),
        "message": template.format(order_id=order_id),
    }


def handle_event(body: bytes) -> dict[str, Any]:
    """Processa um evento do broker e registra a notificação enviada."""
    envelope = EventEnvelope.model_validate_json(body)
    token = correlation_id_var.set(envelope.correlation_id)
    try:
        notification = build_notification(envelope)
        history.append(notification)
        # Simula o envio: este log é a "entrega" ao cliente
        logger.info(
            "e-mail simulado enviado para %s: %s",
            notification["to"],
            notification["message"],
        )
        return notification
    finally:
        correlation_id_var.reset(token)
