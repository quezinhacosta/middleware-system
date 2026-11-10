"""Modelos de dados do domínio de Catálogo e Estoque."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Product(BaseModel):
    """Entidade do produto no catálogo."""

    id: str = Field(description="Identificador único do produto")
    name: str = Field(min_length=1, description="Nome do produto")
    price: float = Field(gt=0.0, description="Preço unitário")
    stock_quantity: int = Field(ge=0, description="Quantidade disponível em estoque")


class ProductCreate(BaseModel):
    """Payload para criação de novo produto."""

    name: str = Field(min_length=1, description="Nome do produto")
    price: float = Field(gt=0.0, description="Preço unitário")
    stock_quantity: int = Field(
        ge=0, default=0, description="Quantidade inicial em estoque"
    )


class ReserveStockRequest(BaseModel):
    """Payload para solicitação de reserva temporária de estoque."""

    quantity: int = Field(gt=0, description="Quantidade solicitada para reserva")


class ReserveStockResponse(BaseModel):
    """Resposta de confirmação de reserva de estoque."""

    product_id: str
    reserved_quantity: int
    remaining_stock: int
    unit_price: float = Field(default=0.0, ge=0.0, description="Preço unitário do item reservado")
    product_name: str | None = Field(default=None, description="Nome do produto")
    status: str = "reserved"
