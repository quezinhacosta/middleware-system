import os
import subprocess
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

COMPOSE = ["docker", "compose"]
BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")  # AJUSTAR: porta do serviço de entrada


def _docker_disponivel():
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=15)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def stack():
    if not _docker_disponivel():
        pytest.skip("Docker não disponível")
    subprocess.run(COMPOSE + ["up", "--build", "-d"], check=True)
    try:
        limite = time.time() + 120
        while time.time() < limite:
            try:
                if httpx.get(f"{BASE_URL}/health/live", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            pytest.fail("Stack não ficou viva em 120s")
        yield BASE_URL
    finally:
        subprocess.run(COMPOSE + ["down", "-v"], check=False)


def test_liveness(stack):
    assert httpx.get(f"{stack}/health/live").json() == {"status": "UP"}


def test_readiness_com_broker_no_ar(stack):
    r = httpx.get(f"{stack}/health/ready", timeout=5)
    assert r.status_code == 200
    assert r.json() == {"status": "UP"}