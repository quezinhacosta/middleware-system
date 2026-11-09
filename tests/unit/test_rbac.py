from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from middleware.auth.rbac import UserIdentity, require_roles

app = FastAPI()


@app.get("/admin-only")
def admin_route(user: UserIdentity = Depends(require_roles(["admin"]))):
    return {"status": "ok", "roles": user.roles}


@app.get("/customer-or-admin")
def mixed_route(user: UserIdentity = Depends(require_roles(["customer", "admin"]))):
    return {"status": "ok", "roles": user.roles}


client = TestClient(app)


def test_acesso_negado_sem_token():
    resp = client.get("/admin-only")
    assert resp.status_code == 401
    assert "ausente" in resp.json()["detail"].lower()


def test_admin_only_rejeita_customer():
    resp = client.get("/admin-only", headers={"Authorization": "Bearer role=customer"})
    assert resp.status_code == 403
    assert "insuficiente" in resp.json()["detail"].lower()


def test_admin_only_aceita_admin():
    resp = client.get("/admin-only", headers={"Authorization": "Bearer role=admin"})
    assert resp.status_code == 200
    assert "admin" in resp.json()["roles"]


def test_rota_mista_aceita_customer():
    resp = client.get(
        "/customer-or-admin", headers={"Authorization": "Bearer role=customer"}
    )
    assert resp.status_code == 200
    assert "customer" in resp.json()["roles"]


def test_rota_mista_aceita_admin():
    resp = client.get(
        "/customer-or-admin", headers={"Authorization": "Bearer role=admin"}
    )
    assert resp.status_code == 200
    assert "admin" in resp.json()["roles"]


def test_rota_mista_rejeita_usuario_sem_permissao():
    resp = client.get(
        "/customer-or-admin", headers={"Authorization": "Bearer role=guest"}
    )
    assert resp.status_code == 403
