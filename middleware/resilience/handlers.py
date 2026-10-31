from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .exceptions import MiddlewareTimeoutError


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