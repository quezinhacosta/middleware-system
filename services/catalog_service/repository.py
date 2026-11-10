"""Repositório thread-safe em memória para o Catálogo e Estoque."""

from __future__ import annotations

import threading
from typing import Any

from services.catalog_service.models import Product, ProductCreate


class ProductNotFoundError(Exception):
    """Lançada quando um produto não existe no repositório."""


class InsufficientStockError(Exception):
    """Lançada quando a quantidade em estoque é menor que a solicitada."""

    def __init__(self, product_id: str, requested: int, available: int) -> None:
        self.product_id = product_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"Estoque insuficiente para produto {product_id}: solicitado {requested}, disponível {available}"
        )


class ProductRepository:
    """Repositório em memória thread-safe com bloqueio atômico para reservas."""

    def __init__(self, initial_products: dict[str, dict[str, Any]] | None = None) -> None:
        self._lock = threading.Lock()
        self._storage: dict[str, Product] = {}

        if initial_products:
            for pid, data in initial_products.items():
                self._storage[str(pid)] = Product(
                    id=str(pid),
                    name=str(data["name"]),
                    price=float(data["price"]),
                    stock_quantity=int(data.get("stock_quantity", 10)),
                )

    def list_products(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        name: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> list[Product]:
        with self._lock:
            items = list(self._storage.values())

        if name:
            name_lower = name.lower()
            items = [p for p in items if name_lower in p.name.lower()]
        if min_price is not None:
            items = [p for p in items if p.price >= min_price]
        if max_price is not None:
            items = [p for p in items if p.price <= max_price]

        return items[skip : skip + limit]

    def count_products(
        self,
        *,
        name: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> int:
        """Retorna o total de produtos correspondentes aos filtros (para paginação)."""
        with self._lock:
            items = list(self._storage.values())

        if name:
            name_lower = name.lower()
            items = [p for p in items if name_lower in p.name.lower()]
        if min_price is not None:
            items = [p for p in items if p.price >= min_price]
        if max_price is not None:
            items = [p for p in items if p.price <= max_price]

        return len(items)

    def get_by_id(self, product_id: str) -> Product | None:
        with self._lock:
            product = self._storage.get(str(product_id))
            if product is None:
                return None
            return Product(
                id=product.id,
                name=product.name,
                price=product.price,
                stock_quantity=product.stock_quantity,
            )

    def create(self, product_in: ProductCreate, custom_id: str | None = None) -> Product:
        with self._lock:
            if custom_id:
                new_id = str(custom_id)
            else:
                numeric_keys = [
                    int(k) for k in self._storage.keys() if k.isdigit()
                ]
                new_id = str(max(numeric_keys, default=0) + 1)

            product = Product(
                id=new_id,
                name=product_in.name,
                price=product_in.price,
                stock_quantity=product_in.stock_quantity,
            )
            self._storage[new_id] = product
            return Product(
                id=product.id,
                name=product.name,
                price=product.price,
                stock_quantity=product.stock_quantity,
            )

    def reserve_stock(self, product_id: str, quantity: int) -> tuple[int, int]:
        """Reserva atomicamente o estoque. Retorna (reserved_quantity, remaining_stock)."""
        pid = str(product_id)
        with self._lock:
            product = self._storage.get(pid)
            if product is None:
                raise ProductNotFoundError(f"Produto {pid} não encontrado")

            if product.stock_quantity < quantity:
                raise InsufficientStockError(pid, quantity, product.stock_quantity)

            product.stock_quantity -= quantity
            remaining = product.stock_quantity
            return quantity, remaining

    def get_raw_dict(self) -> dict[str, dict[str, Any]]:
        """Retorna representação em dicionário para compatibilidade com rotas legadas."""
        with self._lock:
            return {
                p.id: {
                    "id": p.id,
                    "name": p.name,
                    "price": p.price,
                    "in_stock": p.stock_quantity > 0,
                    "stock_quantity": p.stock_quantity,
                }
                for p in self._storage.values()
            }
