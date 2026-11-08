# tests/test_service_auth.py
import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from middleware.auth import service_auth as sa

SECRET = "x" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_SECRET", SECRET)
    sa.get_authenticator.cache_clear()
    yield
    sa.get_authenticator.cache_clear()


@pytest.fixture
def client():
    app = FastAPI()

    @app.post(
        "/products/{product_id}/reserve",
        dependencies=[
            Depends(sa.require_internal_service("catalog-service", {"order-service"}))
        ],
    )
    def reserve(product_id: int):
        return {"reserved": product_id}

    return TestClient(app)


def _token(
    issuer="order-service", audience="catalog-service", secret=SECRET, exp_delta=60
):
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + exp_delta,
        "jti": "t1",
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _post(client, token=None):
    headers = {sa.SERVICE_KEY_HEADER: token} if token else {}
    return client.post("/products/1/reserve", headers=headers)


def test_valid_token_is_accepted(client):
    provider = sa.ServiceTokenProvider("order-service", sa.get_authenticator())
    resp = client.post(
        "/products/1/reserve", headers=provider.auth_headers("catalog-service")
    )
    assert resp.status_code == 200


def test_missing_header_returns_401(client):
    assert _post(client).status_code == 401


def test_wrong_secret_returns_401(client):
    assert _post(client, _token(secret="y" * 40)).status_code == 401


def test_expired_token_returns_401(client):
    assert _post(client, _token(exp_delta=-60)).status_code == 401


def test_wrong_audience_returns_401(client):
    assert _post(client, _token(audience="payment-service")).status_code == 401


def test_alg_none_returns_401(client):
    forged = jwt.encode(
        {"iss": "order-service", "aud": "catalog-service"}, None, algorithm="none"
    )
    assert _post(client, forged).status_code == 401


def test_unauthorized_caller_returns_403(client):
    assert _post(client, _token(issuer="payment-service")).status_code == 403


def test_weak_secret_fails_fast():
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.ServiceAuthenticator("curto")


def test_provider_caches_token_until_near_expiry():
    provider = sa.ServiceTokenProvider("order-service", sa.get_authenticator())
    assert provider.get_token("catalog-service") == provider.get_token(
        "catalog-service"
    )
