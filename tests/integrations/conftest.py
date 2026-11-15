import pytest

from tests.integrations.helpers import (
    BASE_URL,
    compose,
    containers_existentes,
    docker_disponivel,
)


@pytest.fixture(scope="session")
def stack():
    if not docker_disponivel():
        pytest.skip("Docker não está disponível (abra o Docker Desktop)")
    existentes = containers_existentes()
    if existentes:
        pytest.skip(
            f"Já existem containers da stack ({', '.join(sorted(existentes))}). "
            "Rode `make down` antes dos testes de integração."
        )

    try:
        r = compose(
            "up", "--build", "-d", "--wait", "--wait-timeout", "240", timeout=900
        )
        if r.returncode != 0:
            logs = compose("logs", "--tail", "40").stdout
            pytest.fail(
                f"docker compose up falhou:\n{r.stderr[-1500:]}\n--- logs ---\n{logs[-3000:]}"
            )
        yield BASE_URL
    finally:
        # -v remove só os volumes de teste, graças ao override
        compose("down", "-v", "--remove-orphans")
