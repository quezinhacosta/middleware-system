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