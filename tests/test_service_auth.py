import pytest

from middleware.auth import service_auth as sa

SECRET = "x" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_SECRET", SECRET)
    sa.get_authenticator.cache_clear()
    yield
    sa.get_authenticator.cache_clear()


def test_weak_secret_fails_fast():
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.ServiceAuthenticator("curto")


def test_provider_caches_token_until_near_expiry():
    provider = sa.ServiceTokenProvider("order-service", sa.get_authenticator())
    assert provider.get_token("catalog-service") == provider.get_token("catalog-service")