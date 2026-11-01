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