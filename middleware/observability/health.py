import socket

from fastapi import APIRouter, Response, status

# Apenas o router, sem criar uma nova instância de FastAPI() aqui
router = APIRouter(prefix="/health", tags=["Healthcheck"])


def check_rabbitmq() -> bool:
    """
    Testa a conectividade com o RabbitMQ (exemplo usando socket na porta padrão 5672).
    """
    host = "message-broker"
    port = 5672
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except (TimeoutError, OSError):
        return False


@router.get("/live")
def liveness_probe():
    """
    Liveness: Confirma apenas se a aplicação está viva.
    Rota final: GET /health/live
    """
    return {"status": "UP"}


@router.get("/ready")
def readiness_probe(response: Response):
    """
    Readiness: Confirma se as dependências essenciais estão operacionais.
    Rota final: GET /health/ready
    """
    broker_ok = check_rabbitmq()

    if broker_ok:
        return {"status": "UP"}

    # Se a dependência falhar, retorna HTTP 503 conforme especificação
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "DOWN", "details": {"broker": "unreachable"}}
