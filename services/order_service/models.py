"""Modelos de dados do domínio do Order Service."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class OrderStatus(StrEnum):
    """Estados do ciclo de vida de um pedido."""

    PENDING = "PENDING"
    RESERVED = "RESERVED"
    PAYMENT_PROCESSING = "PAYMENT_PROCESSING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class OrderItem(BaseModel):
    """Item pertencente a um pedido."""

    product_id: str = Field(description="Identificador único do produto no catálogo")
    quantity: int = Field(gt=0, description="Quantidade do produto")
    unit_price: float = Field(
        ge=0.0, default=0.0, description="Preço unitário do produto"
    )


class Order(BaseModel):
    """Entidade do pedido."""

    id: str = Field(description="Identificador único do pedido (UUID v4)")
    customer_id: str = Field(description="Identificador do cliente solicitante")
    items: list[OrderItem] = Field(min_length=1, description="Lista de itens do pedido")
    total_amount: float = Field(ge=0.0, description="Valor total acumulado do pedido")
    status: OrderStatus = Field(
        default=OrderStatus.PENDING, description="Status atual do pedido"
    )
    payment_method: str = Field(
        default="PIX", description="Método de pagamento selecionado"
    )


class OrderCreateRequest(BaseModel):
    """Payload para solicitação de criação de um novo pedido."""

    # Suporta items detalhados ou atalho com product_id/quantity (retrocompatível)
    items: list[OrderItem] | None = Field(
        default=None, description="Lista de itens do pedido"
    )
    product_id: int | str | None = Field(
        default=None, description="Atalho de produto único"
    )
    quantity: int = Field(default=1, ge=1, description="Quantidade de produto")
    payment_method: str = Field(default="PIX", description="Forma de pagamento")
