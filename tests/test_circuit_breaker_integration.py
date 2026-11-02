import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from middleware.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenException,
    CircuitState,
)

FAILURE_THRESHOLD = 5
RECOVERY_TIMEOUT = 0.3  # curto para o teste; em produção: 10s


class _CatalogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits += 1
        if self.server.healthy:
            body = b'[{"id": 1, "name": "Teclado"}]'
            self.send_response(200)
        else:
            body = b'{"error": "unavailable"}'
            self.send_response(503)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silencia o log do servidor
        pass


@pytest.fixture
def catalog():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CatalogHandler)
    server.healthy = True
    server.hits = 0
    server.url = f"http://127.0.0.1:{server.server_port}/products"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def breaker(request):
    return CircuitBreaker(
        f"catalog-{request.node.name}",
        failure_threshold=FAILURE_THRESHOLD,
        recovery_timeout=RECOVERY_TIMEOUT,
        expected_exceptions=(OSError,),  # URLError, HTTPError e timeouts
    )


def fetch_products(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=1) as resp:
        return resp.read()


def trip(breaker, catalog):
    """Derruba o catálogo e causa 5 falhas consecutivas."""
    catalog.healthy = False
    for _ in range(FAILURE_THRESHOLD):
        with pytest.raises(OSError):
            breaker.call(fetch_products, catalog.url)


def test_abre_apos_5_falhas_consecutivas(breaker, catalog):
    trip(breaker, catalog)

    assert breaker.state is CircuitState.OPEN
    assert catalog.hits == FAILURE_THRESHOLD


def test_sucesso_zera_contagem_de_falhas(breaker, catalog):
    catalog.healthy = False
    for _ in range(FAILURE_THRESHOLD - 1):
        with pytest.raises(OSError):
            breaker.call(fetch_products, catalog.url)

    catalog.healthy = True
    breaker.call(fetch_products, catalog.url)

    catalog.healthy = False
    for _ in range(FAILURE_THRESHOLD - 1):
        with pytest.raises(OSError):
            breaker.call(fetch_products, catalog.url)
    assert breaker.state is CircuitState.CLOSED


def test_circuito_aberto_rejeita_em_menos_de_2ms_sem_rede(breaker, catalog):
    trip(breaker, catalog)
    hits_before = catalog.hits

    durations = []
    for _ in range(100):
        start = time.perf_counter()
        with pytest.raises(CircuitBreakerOpenException):
            breaker.call(fetch_products, catalog.url)
        durations.append(time.perf_counter() - start)

    assert catalog.hits == hits_before, "nenhuma chamada de rede deveria ocorrer"
    assert sum(durations) / len(durations) < 0.002


def test_half_open_e_fechamento_quando_servico_volta(breaker, catalog):
    trip(breaker, catalog)
    time.sleep(RECOVERY_TIMEOUT + 0.05)
    catalog.healthy = True

    observed = []

    def canary():
        observed.append(breaker.state)
        return fetch_products(catalog.url)

    assert breaker.call(canary)
    assert observed == [CircuitState.HALF_OPEN]
    assert breaker.state is CircuitState.CLOSED


def test_canario_falha_reabre_circuito_por_novo_periodo(breaker, catalog):
    trip(breaker, catalog)
    time.sleep(RECOVERY_TIMEOUT + 0.05)

    with pytest.raises(OSError):  # canário bate no serviço ainda fora do ar
        breaker.call(fetch_products, catalog.url)
    assert breaker.state is CircuitState.OPEN

    hits = catalog.hits
    with pytest.raises(CircuitBreakerOpenException):  # cooldown reiniciou
        breaker.call(fetch_products, catalog.url)
    assert catalog.hits == hits


def test_apenas_um_canario_em_half_open_sob_concorrencia(breaker):
    def boom():
        raise OSError("down")

    for _ in range(FAILURE_THRESHOLD):
        with pytest.raises(OSError):
            breaker.call(boom)
    time.sleep(RECOVERY_TIMEOUT + 0.05)

    executed, rejected = [], []
    barrier = threading.Barrier(20)

    def slow_ok():
        executed.append(1)
        time.sleep(0.1)
        return "ok"

    def worker():
        barrier.wait()
        try:
            breaker.call(slow_ok)
        except CircuitBreakerOpenException:
            rejected.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(executed) == 1
    assert len(rejected) == 19
    assert breaker.state is CircuitState.CLOSED


def test_excecoes_nao_esperadas_nao_abrem_circuito(breaker):
    def regra_de_negocio():
        raise ValueError("produto inválido")

    for _ in range(FAILURE_THRESHOLD * 2):
        with pytest.raises(ValueError):
            breaker.call(regra_de_negocio)

    assert breaker.state is CircuitState.CLOSED


def test_metrica_de_estado(breaker, catalog):
    prometheus = pytest.importorskip("prometheus_client")

    def gauge():
        return prometheus.REGISTRY.get_sample_value(
            "circuit_breaker_state", {"name": breaker.name}
        )

    assert gauge() == 0  # CLOSED
    trip(breaker, catalog)
    assert gauge() == 2  # OPEN

    time.sleep(RECOVERY_TIMEOUT + 0.05)
    catalog.healthy = True
    seen = []
    breaker.call(lambda: seen.append(gauge()) or fetch_products(catalog.url))
    assert seen == [1]  # HALF_OPEN durante o canário
    assert gauge() == 0  # CLOSED de volta
