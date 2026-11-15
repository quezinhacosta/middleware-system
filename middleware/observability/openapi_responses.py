from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="Descrição detalhada do erro ocorrido", examples=["Recurso não encontrado ou acesso negado"])


COMMON_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Requisição inválida ou parâmetros malformados"},
    401: {"model": ErrorResponse, "description": "Token JWT ausente, inválido ou expirado"},
    403: {"model": ErrorResponse, "description": "Usuário autenticado sem permissão (RBAC) para esta operação"},
    404: {"model": ErrorResponse, "description": "Recurso solicitado não encontrado"},
    409: {"model": ErrorResponse, "description": "Conflito de estado ou estoque insuficiente"},
    503: {"model": ErrorResponse, "description": "Serviço dependente indisponível (Circuit Breaker aberto)"},
}
