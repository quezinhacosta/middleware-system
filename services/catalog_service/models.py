from pydantic import BaseModel, Field


class Product(BaseModel):
    id: str
    name: str
    price: float = Field(gt=0, description="Preço do produto deve ser maior que zero")
    stock_quantity: int = Field(
        ge=0, description="Quantidade em estoque deve ser maior ou igual a zero"
    )


class ReserveRequest(BaseModel):
    quantity: int = Field(
        gt=0, description="Quantidade a ser reservada deve ser maior que zero"
    )


class ReserveResponse(BaseModel):
    product_id: str
    reserved_quantity: int
    remaining_stock: int
