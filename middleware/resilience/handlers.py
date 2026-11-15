from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .exceptions import MiddlewareTimeoutError, ServiceUnavailableException


def register_timeout_handler(app: FastAPI) -> None:
    @app.exception_handler(MiddlewareTimeoutError)
    async def _timeout_handler(request: Request, exc: MiddlewareTimeoutError):
        # Resposta genérica: URL/detalhes internos ficam só no log.
        return JSONResponse(
            status_code=504,
            content={
                "error": "gateway_timeout",
                "detail": "Serviço dependente excedeu o tempo limite.",
                "timeout_seconds": exc.timeout,
            },
        )


def register_service_unavailable_handler(app: FastAPI) -> None:
    @app.exception_handler(ServiceUnavailableException)
    async def _service_unavailable_handler(
        request: Request, exc: ServiceUnavailableException
    ):
        return JSONResponse(
            status_code=503,
            content={
                "error": "service_unavailable",
                "detail": "Serviço dependente indisponível ou inacessível.",
            },
        )


def register_resilience_handlers(app: FastAPI) -> None:
    """Registra handlers de resiliência (503 Service Unavailable e 504 Gateway Timeout)."""
    register_timeout_handler(app)
    register_service_unavailable_handler(app)
