from fastapi import FastAPI

app = FastAPI(
    title="Order Service",
    description="Microsserviço de gestão e checkout de pedidos do e-commerce",
    version="0.1.0",
)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "order-service", "status": "running"}


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "healthy", "service": "order-service"}
