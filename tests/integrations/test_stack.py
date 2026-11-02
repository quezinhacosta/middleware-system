import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

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


# ---------- helpers ----------

def _run(args, timeout=600):
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


def _docker(*args, timeout=60):
    return _run(["docker", *args], timeout=timeout)


def _compose(*args, timeout=600):
    return _run(
        ["docker", "compose", "-f", str(COMPOSE_BASE), "-f", str(COMPOSE_TEST), *args],
        timeout=timeout,
    )


def _docker_disponivel():
    try:
        return _docker("info", timeout=15).returncode == 0
    except Exception:
        return False


def _containers_existentes():
    saida = _docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
    return set(saida) & set(CONTAINERS)


def _status_saude(container):
    return _docker("inspect", "-f", "{{.State.Health.Status}}", container).stdout.strip()


def _esperar_saudaveis(timeout=180):
    limite = time.time() + timeout
    status = {}
    while time.time() < limite:
        status = {c: _status_saude(c) for c in CONTAINERS}
        if all(s == "healthy" for s in status.values()):
            return
        time.sleep(3)
    pytest.fail(f"Containers não ficaram saudáveis em {timeout}s: {status}")


def _esperar_resposta(caminho, status_esperado, timeout=60):
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
    pytest.fail(f"{caminho} não retornou {status_esperado} em {timeout}s (último: {ultima})")


def _http_dentro_do_container(container, url):
    """Faz GET de dentro de um container (rede interna do compose) e devolve o status."""
    codigo = f"import urllib.request; print(urllib.request.urlopen('{url}', timeout=3).status)"
    r = _docker("exec", container, "python", "-c", codigo)
    return r.stdout.strip()


# ---------- fixture ----------

@pytest.fixture(scope="session")
def stack():
    if not _docker_disponivel():
        pytest.skip("Docker não está disponível (abra o Docker Desktop)")
    existentes = _containers_existentes()
    if existentes:
        pytest.skip(
            f"Já existem containers da stack ({', '.join(sorted(existentes))}). "
            "Rode `make down` antes dos testes de integração."
        )

    try:
        r = _compose("up", "--build", "-d", "--wait", "--wait-timeout", "240", timeout=900)
        if r.returncode != 0:
            logs = _compose("logs", "--tail", "40").stdout
            pytest.fail(f"docker compose up falhou:\n{r.stderr[-1500:]}\n--- logs ---\n{logs[-3000:]}")
        yield BASE_URL
    finally:
        # -v remove só o volume de teste (rabbitmq_data_test), graças ao override
        _compose("down", "-v", "--remove-orphans")


# ---------- testes ----------

def test_todos_os_containers_ficam_saudaveis(stack):
    status = {c: _status_saude(c) for c in CONTAINERS}
    assert status == dict.fromkeys(CONTAINERS, "healthy")


def test_liveness_do_order_service(stack):
    r = httpx.get(f"{stack}/health/live", timeout=5)
    assert r.status_code == 200
    assert r.json() == {"status": "UP"}


def test_readiness_do_order_service_com_broker_no_ar(stack):
    r = httpx.get(f"{stack}/health/ready", timeout=5)
    assert r.status_code == 200
    assert r.json() == {"status": "UP"}


@pytest.mark.parametrize(
    "container,url",
    [
        ("catalog-service", "http://localhost:8001/health/live"),
        ("payment-service", "http://localhost:8002/health/live"),
    ],
)
def test_servicos_internos_respondem_dentro_da_rede(stack, container, url):
    assert _http_dentro_do_container(container, url) == "200"


def test_order_service_alcanca_catalog_e_payment_pela_rede_interna(stack):
    assert _http_dentro_do_container("order-service", "http://catalog-service:8001/health/live") == "200"
    assert _http_dentro_do_container("order-service", "http://payment-service:8002/health/live") == "200"


@pytest.mark.parametrize("porta", [8001, 8002])
def test_catalog_e_payment_nao_sao_publicados_no_host(stack, porta):
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://localhost:{porta}/health/live", timeout=2)


def test_broker_usa_as_credenciais_de_teste(stack):
    ok = httpx.get(
        f"{MGMT_URL}/api/overview",
        auth=(TEST_ENV["RABBITMQ_DEFAULT_USER"], TEST_ENV["RABBITMQ_DEFAULT_PASS"]),
        timeout=10,
    )
    assert ok.status_code == 200
    negado = httpx.get(f"{MGMT_URL}/api/overview", auth=("guest", "guest"), timeout=10)
    assert negado.status_code == 401


# Este teste altera o estado da stack: deixe-o SEMPRE por último no arquivo.
def test_readiness_degrada_quando_o_broker_cai_e_se_recupera(stack):
    _docker("stop", "message-broker")
    try:
        r = _esperar_resposta("/health/ready", 503, timeout=40)
        assert r.json() == {"status": "DOWN", "details": {"broker": "unreachable"}}
        # liveness continua OK: o serviço está vivo, só não está pronto
        assert httpx.get(f"{stack}/health/live", timeout=5).status_code == 200
    finally:
        _docker("start", "message-broker")
        _esperar_saudaveis(timeout=180)
    assert _esperar_resposta("/health/ready", 200, timeout=60).json() == {"status": "UP"}
