import os
import threading
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from middleware.observability.health import router as health_router
from src.consumer import start_consuming


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Sobe o consumer do RabbitMQ em uma thread em background
    enquanto o FastAPI atende os endpoints /health/*.
    """
    consumer_thread = threading.Thread(
        target=start_consuming,
        name="rabbitmq-consumer",
        daemon=True,  # encerra junto com o processo principal
    )
    consumer_thread.start()
    yield


app = FastAPI(title="Payment Service", lifespan=lifespan)
app.include_router(health_router)


if __name__ == "__main__":
    print("=== Iniciando Payment Service (Worker + Health API) ===")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8002")))