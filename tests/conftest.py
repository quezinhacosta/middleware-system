import pytest

# Segredo fake (>= 32 bytes), usado só nos testes. Nunca é credencial real.
TEST_SECRET = "segredo-de-teste-com-mais-de-32-bytes-0123456789"


@pytest.fixture
def secret():
    return TEST_SECRET


@pytest.fixture
def s2s_env(monkeypatch):
    """Configura o ambiente do S2S e limpa o singleton lru_cache antes/depois."""
    from middleware.auth import service_auth

    monkeypatch.setenv("INTERNAL_SERVICE_SECRET", TEST_SECRET)
    monkeypatch.delenv("INTERNAL_TOKEN_TTL", raising=False)
    service_auth.get_authenticator.cache_clear()
    yield
    service_auth.get_authenticator.cache_clear()