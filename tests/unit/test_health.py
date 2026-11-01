import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.observability import health


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health.router)
    return TestClient(app)


def test_liveness_sempre_up(client):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "UP"}


def test_readiness_up_quando_broker_responde(client, monkeypatch):
    monkeypatch.setattr(health, "check_rabbitmq", lambda: True)
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "UP"}


def test_readiness_503_quando_broker_cai(client, monkeypatch):
    monkeypatch.setattr(health, "check_rabbitmq", lambda: False)
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "DOWN", "details": {"broker": "unreachable"}}


def test_check_rabbitmq_true_quando_conecta(monkeypatch):
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: Conn())
    assert health.check_rabbitmq() is True


@pytest.mark.parametrize("erro", [socket.timeout, ConnectionRefusedError, OSError])
def test_check_rabbitmq_false_em_falha_de_rede(monkeypatch, erro):
    def falha(*a, **k):
        raise erro()

    monkeypatch.setattr(socket, "create_connection", falha)
    assert health.check_rabbitmq() is False