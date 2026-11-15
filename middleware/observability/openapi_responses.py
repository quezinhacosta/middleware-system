"""Respostas padronizadas para documentação OpenAPI/Swagger dos microsserviços."""

from typing import Any

COMMON_RESPONSES: dict[int, dict[str, Any]] = {
    400: {"description": "Requisição inválida ou parâmetros ausentes/malformados."},
    401: {"description": "Não autorizado. Token de autenticação ausente, inválido ou expirado."},
    403: {"description": "Acesso negado. O usuário não possui a permissão/role necessária."},
    404: {"description": "Recurso não encontrado."},
    409: {"description": "Conflito de estado de recurso (ex.: estoque insuficiente ou duplicação)."},
    500: {"description": "Erro interno do servidor."},
    503: {"description": "Serviço temporariamente indisponível ou Circuit Breaker aberto."},
    504: {"description": "Gateway Timeout ao se comunicar com serviços dependentes."},
}
