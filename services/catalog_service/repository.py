from threading import Lock

from src.models import Product


class ProductRepository:
    def __init__(self) -> None:
        self._lock = Lock()
        self._products: dict[str, Product] = {}
        self._seed_data()

    def _seed_data(self) -> None:
        seeds = [
            Product(
                id="prod-001",
                name="Teclado Mecânico RGB",
                price=250.00,
                stock_quantity=15,
            ),
            Product(
                id="prod-002",
                name="Mouse Sem Fio Ergoclick",
                price=120.00,
                stock_quantity=30,
            ),
            Product(
                id="prod-003",
                name="Monitor Ultrawide 29",
                price=1350.00,
                stock_quantity=5,
            ),
            Product(
                id="prod-004", name="Headset Gamer 7.1", price=320.00, stock_quantity=0
            ),
        ]
        for product in seeds:
            self._products[product.id] = product

    def get_all(
        self, skip: int = 0, limit: int = 10, search: str | None = None
    ) -> tuple[list[Product], int]:
        with self._lock:
            products = list(self._products.values())
            if search:
                products = [p for p in products if search.lower() in p.name.lower()]
            total = len(products)
            paginated = products[skip : skip + limit]
            return paginated, total

    def get_by_id(self, product_id: str) -> Product | None:
        with self._lock:
            return self._products.get(product_id)

    def reserve_stock(
        self, product_id: str, quantity: int
    ) -> tuple[bool, Product | None, str]:
        with self._lock:
            product = self._products.get(product_id)
            if not product:
                return False, None, "Product not found"

            if product.stock_quantity < quantity:
                return False, product, "Insufficient stock"

            # Decrementa o estoque de forma atômica dentro do lock
            product.stock_quantity -= quantity
            return True, product, "Stock reserved successfully"


# Instância global do repositório
repository = ProductRepository()
