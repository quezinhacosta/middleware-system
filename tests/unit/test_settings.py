import pytest
from pydantic import ValidationError

from middleware.config.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch, tmp_path):
    """Isola cada teste: sem env vars dos campos, sem .env real (cwd temporário)."""
    for campo in Settings.model_fields:
        monkeypatch.delenv(campo, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------- defaults ----------

def test_defaults():
    s = Settings()
    assert s.SERVICE_NAME == "middleware-service"
    assert s.PORT == 8000
    assert s.ENVIRONMENT == "development"
    assert s.LOG_LEVEL == "INFO"
    assert s.RABBITMQ_HOST == "message-broker"
    assert s.RABBITMQ_PORT == 5672
    assert s.JWT_ALGORITHM == "HS256"
    assert s.JWT_EXPIRATION_MINUTES == 60
    assert s.HTTP_TIMEOUT_SECONDS == 3.0
    assert s.MAX_RETRIES == 3
    assert s.CIRCUIT_BREAKER_FAILURE_THRESHOLD == 5
    assert s.ORDER_SERVICE_URL == "http://order-service:8000"
    assert s.CATALOG_SERVICE_URL == "http://catalog-service:8001"


# ---------- leitura de env vars e .env ----------

def test_env_var_sobrescreve_default(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("ENVIRONMENT", "production")
    s = Settings()
    assert s.PORT == 9000
    assert s.ENVIRONMENT == "production"


def test_conversao_de_tipos(monkeypatch):
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("MAX_RETRIES", "7")
    s = Settings()
    assert s.HTTP_TIMEOUT_SECONDS == 2.5
    assert s.MAX_RETRIES == 7


def test_le_arquivo_dotenv(tmp_path):
    (tmp_path / ".env").write_text("PORT=9100\nSERVICE_NAME=do-dotenv\n", encoding="utf-8")
    s = Settings()
    assert s.PORT == 9100
    assert s.SERVICE_NAME == "do-dotenv"


def test_env_var_tem_prioridade_sobre_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PORT=9100\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "9200")
    assert Settings().PORT == 9200


def test_variaveis_desconhecidas_sao_ignoradas(tmp_path):
    (tmp_path / ".env").write_text("VARIAVEL_QUE_NAO_EXISTE=1\n", encoding="utf-8")
    s = Settings()
    assert not hasattr(s, "VARIAVEL_QUE_NAO_EXISTE")


# ---------- validação ----------

@pytest.mark.parametrize(
    "campo,valor",
    [
        ("PORT", "80"),
        ("PORT", "70000"),
        ("PORT", "abc"),
        ("ENVIRONMENT", "staging"),
        ("LOG_LEVEL", "TRACE"),
        ("RABBITMQ_PORT", "0"),
        ("JWT_SECRET_KEY", "curta"),
        ("JWT_EXPIRATION_MINUTES", "0"),
        ("HTTP_TIMEOUT_SECONDS", "0"),
        ("MAX_RETRIES", "-1"),
        ("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "0"),
    ],
)
def test_valores_invalidos_sao_rejeitados(monkeypatch, campo, valor):
    monkeypatch.setenv(campo, valor)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("porta", ["1024", "65535"])
def test_limites_validos_da_porta(monkeypatch, porta):
    monkeypatch.setenv("PORT", porta)
    assert Settings().PORT == int(porta)


def test_zero_retentativas_e_valido(monkeypatch):
    monkeypatch.setenv("MAX_RETRIES", "0")
    assert Settings().MAX_RETRIES == 0


# ---------- get_settings ----------

def test_get_settings_retorna_singleton(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    primeira = get_settings()
    assert get_settings() is primeira
    assert primeira.PORT == 9000


def test_get_settings_usa_cache_ate_limpar(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    assert get_settings().PORT == 9000
    monkeypatch.setenv("PORT", "9001")
    assert get_settings().PORT == 9000  # ainda em cache
    get_settings.cache_clear()
    assert get_settings().PORT == 9001