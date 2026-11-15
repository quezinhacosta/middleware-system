## Teste de Caos (Tolerância a Falhas)

**Pré-requisitos:** `docker`, `curl`, `jq` e a stack no ar (`docker compose up -d`).

```bash
./scripts/chaos_test.sh
# Ajustes opcionais:
OUTAGE_SECONDS=30 ORDER_URL=http://localhost:8000 ./scripts/chaos_test.sh
```

**Roteiro da apresentação**
1. Em um terminal, rode `docker compose logs -f order-service`.
2. Em outro, rode o script e acompanhe o log JSON (`event`: `fault_injected`, `contingency_evidence`, `healed`).
3. Cenário 1: `catalog-service` cai, o Order Service dispara retries e o Circuit Breaker abre (fallback). Ao religar, o breaker fecha e os pedidos voltam a ser aceitos.
4. Cenário 2: `payment-service` cai, as mensagens ficam retidas em `payment_queue` (ver http://localhost:15672). Ao religar, a fila é drenada.
5. Resultado final: `summary` com `PASS`/`FAIL` e exit code 0/1. O log fica em `logs/chaos_test_*.log`.

---

## Comportamento de Contingência e Fallback (`@with_fallback`)

Quando uma falha irrecuperável ocorre (timeout, recusa de conexão ou abertura do `CircuitBreaker`), o `order-service` aciona o decorator `@with_fallback(fallback_handler)` (`middleware/resilience/fallback.py`), garantindo resposta **HTTP 200** com degradação graciosa e registrando um alerta estruturado com a tag `FALLBACK_TRIGGERED`.

| Endpoint (`order-service`) | Serviço Parceiro | Comportamento Normal | Comportamento em Contingência (Fallback) |
| :--- | :--- | :--- | :--- |
| `GET /catalog/recommendations` | `catalog-service` | Lista de produtos recomendados (`degraded: false`). | Retorna HTTP 200 com lista vazia `items: []` e flag `degraded: true`. |
| `GET /catalog/products/{id}` | `catalog-service` | Dados estendidos do produto (`degraded: false`). | Retorna HTTP 200 com versão em cache local (`source: "local_cache"`) ou metadados básicos e flag `degraded: true`. |
| `POST /payments` | `payment-service` | Processamento síncrono do pagamento (`degraded: false`). | Retorna HTTP 200 com confirmação de agendamento (`status: "PAYMENT_PENDING"`), `degraded: true` e publica evento no broker para reprocessamento em background. |
| `POST /orders` | `catalog-service` + `payment-service` | Valida item no catálogo e processa pagamento. | Consulta cache local do produto e agenda pagamento com `status: "PAYMENT_PENDING"` e `degraded: true`. |