import json
import os
import subprocess
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_BASE = ROOT / "docker-compose.yml"
COMPOSE_TEST = Path(__file__).parent / "docker-compose.test.yml"

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")
MGMT_URL = "http://localhost:15672"
CONTAINERS = ["message-broker", "order-service", "catalog-service", "payment-service"]

# Credenciais FAKE, só para os testes: o compose as exige e elas substituem o .env.
TEST_ENV = {
    "RABBITMQ_DEFAULT_USER": "teste_user",
    "RABBITMQ_DEFAULT_PASS": "teste_pass_nao_e_de_producao",
    "JWT_SECRET_KEY": "segredo-de-teste-integracao-com-mais-de-32-bytes",
    "ENVIRONMENT": "testing",
}
MGMT_AUTH = (TEST_ENV["RABBITMQ_DEFAULT_USER"], TEST_ENV["RABBITMQ_DEFAULT_PASS"])


# ---------- docker ----------


def run(args, timeout=600):
    return subprocess.run(
        args,
        cwd=ROOT,
        env={**os.environ, **TEST_ENV},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def docker(*args, timeout=60):
    return run(["docker", *args], timeout=timeout)


def compose(*args, timeout=600):
    return run(
        ["docker", "compose", "-f", str(COMPOSE_BASE), "-f", str(COMPOSE_TEST), *args],
        timeout=timeout,
    )


def docker_disponivel():
    try:
        return docker("info", timeout=15).returncode == 0
    except Exception:
        return False


def containers_existentes():
    saida = docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
    return set(saida) & set(CONTAINERS)


def status_saude(container):
    return docker("inspect", "-f", "{{.State.Health.Status}}", container).stdout.strip()


def esperar(condicao, timeout, descricao, intervalo=2):
    """Repete `condicao()` até ficar verdadeira; falha o teste se estourar o prazo."""
    limite = time.time() + timeout
    while time.time() < limite:
        try:
            if condicao():
                return
        except httpx.HTTPError:
            pass
        time.sleep(intervalo)
    pytest.fail(f"Tempo esgotado ({timeout}s) esperando: {descricao}")


def esperar_saudaveis(timeout=180):
    limite = time.time() + timeout
    status = {}
    while time.time() < limite:
        status = {c: status_saude(c) for c in CONTAINERS}
        if all(s == "healthy" for s in status.values()):
            return
        time.sleep(3)
    pytest.fail(f"Containers não ficaram saudáveis em {timeout}s: {status}")


def esperar_resposta(caminho, status_esperado, timeout=60):
    limite = time.time() + timeout
    ultima = None
    while time.time() < limite:
        try:
            ultima = httpx.get(f"{BASE_URL}{caminho}", timeout=5)
            if ultima.status_code == status_esperado:
                return ultima
        except httpx.HTTPError:
            pass
        time.sleep(2)
    pytest.fail(
        f"{caminho} não retornou {status_esperado} em {timeout}s (último: {ultima})"
    )


def http_dentro_do_container(container, url):
    """GET de dentro de um container (rede interna do compose); devolve o status."""
    codigo = f"import urllib.request; print(urllib.request.urlopen('{url}', timeout=3).status)"
    return docker("exec", container, "python", "-c", codigo).stdout.strip()


def logs_do_container(container):
    r = docker("logs", container, timeout=60)
    return r.stdout + r.stderr  # o Docker separa stdout e stderr do container


# ---------- RabbitMQ (API de management, porta 15672 publicada no host) ----------


def mensagens_na_fila(nome):
    """Total de mensagens na fila (0 se ela não existe). As estatísticas do RabbitMQ
    atualizam a cada ~5s, então leia sempre depois de esperar."""
    r = httpx.get(
        f"{MGMT_URL}/api/queues/%2F/{urllib.parse.quote(nome, safe='')}",
        auth=MGMT_AUTH,
        timeout=10,
    )
    if r.status_code == 404:
        return 0
    r.raise_for_status()
    return r.json().get("messages", 0)


def fila_existe(nome):
    r = httpx.get(
        f"{MGMT_URL}/api/queues/%2F/{urllib.parse.quote(nome, safe='')}",
        auth=MGMT_AUTH,
        timeout=10,
    )
    return r.status_code == 200


def publicar_na_fila(fila, corpo):
    """Publica `corpo` (str) na fila pela exchange padrão. Devolve True se foi roteada."""
    r = httpx.post(
        f"{MGMT_URL}/api/exchanges/%2F/amq.default/publish",
        auth=MGMT_AUTH,
        timeout=10,
        json={
            "properties": {"delivery_mode": 2, "content_type": "application/json"},
            "routing_key": fila,
            "payload": corpo,
            "payload_encoding": "string",
        },
    )
    r.raise_for_status()
    return r.json().get("routed", False)


def publicar_evento(fila, evento: dict):
    return publicar_na_fila(fila, json.dumps(evento))
