from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Modelo central de configurações com validação tipada e leitura hierárquica."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Identificação do Microsserviço
    SERVICE_NAME: str = Field(
        default="middleware-service",
        description="Identificador único do microsserviço",
    )
    PORT: int = Field(
        default=8000,
        ge=1024,
        le=65535,
        description="Porta de escuta do servidor HTTP",
    )
    ENVIRONMENT: Literal["development", "testing", "production"] = Field(
        default="development",
        description="Ambiente de execução",
    )

    # Observabilidade
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Nível mínimo para emissão de logs estruturados",
    )

    # Mensageria / RabbitMQ
    RABBITMQ_HOST: str = Field(
        default="message-broker",
        description="Endereço de rede do broker RabbitMQ",
    )
    RABBITMQ_PORT: int = Field(
        default=5672,
        ge=1,
        le=65535,
        description="Porta AMQP do broker",
    )
    RABBITMQ_USER: str = Field(
        default="guest",
        description="Usuário de autenticação no broker",
    )
    RABBITMQ_PASS: str = Field(
        default="guest",
        description="Senha de autenticação no broker",
    )

    # Segurança e Autenticação (JWT)
    JWT_SECRET_KEY: str = Field(
        default="super-secret-distributed-middleware-key-change-in-production-2026",
        min_length=16,
        description="Chave secreta para assinatura criptográfica de tokens JWT",
    )
    JWT_ALGORITHM: str = Field(
        default="HS256",
        description="Algoritmo de criptografia do token JWT",
    )
    JWT_EXPIRATION_MINUTES: int = Field(
        default=60,
        gt=0,
        description="Tempo de validade do token em minutos",
    )

    # Resiliência
    HTTP_TIMEOUT_SECONDS: float = Field(
        default=3.0,
        gt=0.0,
        description="Timeout máximo em segundos para requisições HTTP entre serviços",
    )
    MAX_RETRIES: int = Field(
        default=3,
        ge=0,
        description="Número máximo de retentativas para erros de rede transitórios",
    )
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = Field(
        default=5,
        ge=1,
        description="Quantidade de falhas consecutivas para abrir o Circuit Breaker",
    )

    # URLs dos Serviços Interconectados
    ORDER_SERVICE_URL: str = Field(
        default="http://order-service:8000",
        description="URL base do Order Service na rede Docker",
    )
    CATALOG_SERVICE_URL: str = Field(
        default="http://catalog-service:8001",
        description="URL base do Catalog Service na rede Docker",
    )


@lru_cache
def get_settings() -> Settings:
    """Retorna uma instância única (singleton em cache) das configurações validadas."""
    return Settings()
