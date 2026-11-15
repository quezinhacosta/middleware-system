import time
import pytest

from middleware.auth.token_provider import (
    AuthenticatedUser,
    InvalidTokenError,
    TokenExpiredError,
    TokenManager,
)


def test_authenticated_user_has_role():
    user = AuthenticatedUser(user_id="u123", roles=["customer", "admin"])
    assert user.has_role("customer") is True
    assert user.has_role("admin") is True
    assert user.has_role("internal_service") is False


def test_geracao_e_verificacao_de_token_valido():
    manager = TokenManager(secret_key="secret-chave-de-teste-muito-segura-123456", expiration_minutes=30)
    token = manager.create_token(user_id="user_abc", roles=["customer", "vip"])
    assert isinstance(token, str)

    claims = manager.verify_token(token)
    assert claims["sub"] == "user_abc"
    assert claims["roles"] == ["customer", "vip"]
    assert claims["iss"] == "ecommerce-auth"
    assert claims["exp"] > claims["iat"]

    user = manager.get_user_from_token(token)
    assert user.user_id == "user_abc"
    assert user.roles == ["customer", "vip"]


def test_rejeicao_de_token_expirado():
    manager = TokenManager(secret_key="secret-chave-de-teste-muito-segura-123456")
    # Expira imediatamente (-10 segundos)
    token = manager.create_token(user_id="user_exp", expires_in_seconds=-10)

    with pytest.raises(TokenExpiredError) as exc:
        manager.verify_token(token)
    assert "expirado" in str(exc.value).lower()


def test_rejeicao_de_token_com_chave_incorreta():
    manager_orig = TokenManager(secret_key="chave-correta-super-segura-123456789")
    manager_fake = TokenManager(secret_key="chave-incorreta-diferente-987654321")

    token = manager_orig.create_token(user_id="user_hack")

    with pytest.raises(InvalidTokenError) as exc:
        manager_fake.verify_token(token)
    assert "inválida" in str(exc.value).lower()


def test_rejeicao_de_token_com_emissor_invalido():
    manager_outro_emissor = TokenManager(
        secret_key="mesma-chave-segura-de-teste-123456789",
        issuer="outro-emissor-desconhecido",
    )
    manager_padrao = TokenManager(
        secret_key="mesma-chave-segura-de-teste-123456789",
        issuer="ecommerce-auth",
    )

    token = manager_outro_emissor.create_token(user_id="user_iss")

    with pytest.raises(InvalidTokenError) as exc:
        manager_padrao.verify_token(token)
    assert "emissor" in str(exc.value).lower() or "inválido" in str(exc.value).lower()


def test_rejeicao_de_token_malformado():
    manager = TokenManager()
    with pytest.raises(InvalidTokenError):
        manager.verify_token("token.malformado.invalido")

    with pytest.raises(InvalidTokenError):
        manager.verify_token("plain-string-sem-pontos")


def test_endpoint_mock_auth_login():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from middleware.auth.token_provider import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    client = TestClient(app)

    payload = {
        "username": "joao_cliente",
        "roles": ["customer"],
        "expires_in_seconds": 1800,
    }
    resp = client.post("/auth/login", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["token_type"] == "bearer"
    assert data["user_id"] == "joao_cliente"
    assert data["roles"] == ["customer"]
    assert data["expires_in"] == 1800
    assert "access_token" in data

    # Valida que o token retornado é verificável pelo TokenManager
    manager = TokenManager()
    claims = manager.verify_token(data["access_token"])
    assert claims["sub"] == "joao_cliente"
    assert claims["roles"] == ["customer"]
