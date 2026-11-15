import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from middleware.auth.authenticator import AuthMiddleware
from middleware.auth.context import auth_token_var
from middleware.auth.token_provider import AuthenticatedUser, TokenManager


def create_test_app(token_manager: TokenManager, **kwargs) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware, token_manager=token_manager, **kwargs)

    @app.get("/public")
    def public_route(request: Request):
        user = getattr(request.state, "user", None)
        return {
            "status": "public_ok",
            "user_id": user.user_id if user else None,
        }

    @app.get("/orders")
    def orders_route(request: Request):
        user: AuthenticatedUser = request.state.user
        return {
            "status": "orders_ok",
            "user_id": user.user_id,
            "roles": user.roles,
            "token_in_context": auth_token_var.get(),
        }

    @app.post("/admin/items")
    def admin_route(request: Request):
        user: AuthenticatedUser = request.state.user
        return {
            "status": "admin_ok",
            "user_id": user.user_id,
            "roles": user.roles,
        }

    return app


@pytest.fixture
def token_manager():
    return TokenManager(secret_key="segredo-de-teste-para-o-middleware-123456")


@pytest.fixture
def client(token_manager):
    app = create_test_app(
        token_manager,
        exempt_paths={"/public", "/health"},
        allow_mock_tokens=False,
    )
    return TestClient(app)


def test_rota_protegida_sem_header_authorization_retorna_401(client):
    resp = client.get("/orders")
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "unauthorized"
    assert "ausente" in data["detail"].lower()


def test_rota_protegida_com_token_valido_avanca_e_injeta_user(client, token_manager):
    token = token_manager.create_token(
        user_id="cliente_123",
        roles=["customer", "vip"],
    )
    resp = client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "orders_ok"
    assert data["user_id"] == "cliente_123"
    assert data["roles"] == ["customer", "vip"]
    assert data["token_in_context"] == token


def test_rota_protegida_com_token_expirado_retorna_401(client, token_manager):
    expired_token = token_manager.create_token(
        user_id="cliente_exp",
        expires_in_seconds=-5,
    )
    resp = client.get("/orders", headers={"Authorization": f"Bearer {expired_token}"})
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "unauthorized"
    assert "expirado" in data["detail"].lower()


def test_rota_protegida_com_assinatura_incorreta_retorna_401(client):
    other_manager = TokenManager(secret_key="outro-segredo-completamente-diferente-xyz")
    tampered_token = other_manager.create_token(user_id="hacker")

    resp = client.get("/orders", headers={"Authorization": f"Bearer {tampered_token}"})
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "unauthorized"


def test_rota_protegida_com_token_malformado_retorna_401(client):
    resp = client.get(
        "/orders", headers={"Authorization": "Bearer token.invalido.lixo"}
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"] == "unauthorized"


def test_rota_publica_acessivel_sem_token(client):
    resp = client.get("/public")
    assert resp.status_code == 200
    assert resp.json()["status"] == "public_ok"
    assert resp.json()["user_id"] is None


def test_rota_publica_preenche_user_se_token_fornecido(client, token_manager):
    token = token_manager.create_token(user_id="user_public_identificado")
    resp = client.get("/public", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "user_public_identificado"


def test_suporte_a_tokens_mock_quando_habilitado(token_manager):
    app = create_test_app(
        token_manager,
        exempt_paths={"/public"},
        allow_mock_tokens=True,
    )
    client = TestClient(app)

    resp = client.get(
        "/orders",
        headers={"Authorization": "Bearer role=admin,sub=admin_boss"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == "admin_boss"
    assert "admin" in data["roles"]
