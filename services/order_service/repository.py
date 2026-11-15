"""Repositório thread-safe em memória para Orders."""

from __future__ import annotations

import threading

from services.order_service.models import Order, OrderStatus


class OrderRepository:
    """Repositório em memória thread-safe para o ciclo de vida dos pedidos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orders: dict[str, Order] = {}

    def save(self, order: Order) -> Order:
        """Salva ou atualiza um pedido no repositório."""
        with self._lock:
            cloned = Order(
                id=order.id,
                customer_id=order.customer_id,
                items=[item.model_copy() for item in order.items],
                total_amount=order.total_amount,
                status=order.status,
                payment_method=order.payment_method,
            )
            self._orders[order.id] = cloned
            return Order(
                id=cloned.id,
                customer_id=cloned.customer_id,
                items=[item.model_copy() for item in cloned.items],
                total_amount=cloned.total_amount,
                status=cloned.status,
                payment_method=cloned.payment_method,
            )

    def get_by_id(self, order_id: str) -> Order | None:
        """Retorna uma cópia do pedido por ID ou None se não existir."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return None
            return Order(
                id=order.id,
                customer_id=order.customer_id,
                items=[item.model_copy() for item in order.items],
                total_amount=order.total_amount,
                status=order.status,
                payment_method=order.payment_method,
            )

    def list_by_customer(self, customer_id: str) -> list[Order]:
        """Lista os pedidos pertencentes a um cliente específico."""
        with self._lock:
            return [
                Order(
                    id=order.id,
                    customer_id=order.customer_id,
                    items=[item.model_copy() for item in order.items],
                    total_amount=order.total_amount,
                    status=order.status,
                    payment_method=order.payment_method,
                )
                for order in self._orders.values()
                if order.customer_id == customer_id
            ]

    def list_all(self) -> list[Order]:
        """Lista todos os pedidos existentes no repositório."""
        with self._lock:
            return [
                Order(
                    id=order.id,
                    customer_id=order.customer_id,
                    items=[item.model_copy() for item in order.items],
                    total_amount=order.total_amount,
                    status=order.status,
                    payment_method=order.payment_method,
                )
                for order in self._orders.values()
            ]

    def update_status(self, order_id: str, new_status: OrderStatus) -> Order | None:
        """Atualiza atomicamente o status de um pedido."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return None
            order.status = new_status
            return Order(
                id=order.id,
                customer_id=order.customer_id,
                items=[item.model_copy() for item in order.items],
                total_amount=order.total_amount,
                status=order.status,
                payment_method=order.payment_method,
            )

    def clear(self) -> None:
        """Limpa todo o repositório (útil para testes unitários)."""
        with self._lock:
            self._orders.clear()
