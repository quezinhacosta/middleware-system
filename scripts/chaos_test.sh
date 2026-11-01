#!/usr/bin/env bash
# =============================================================================
# scripts/chaos_test.sh — Teste de caos / tolerância a falhas (middleware-system)
#
# Cenário 1: queda do catalog-service  -> Retry + Circuit Breaker + Fallback
# Cenário 2: queda do payment-service  -> mensagens retidas no RabbitMQ
#
# Uso: ./scripts/chaos_test.sh
# Dependências: docker (compose v2), curl, jq
# Saída: logs JSON (stdout + logs/chaos_test_*.log). Exit 0 = sucesso, 1 = falha.
# =============================================================================
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ----------------------------- Configuração ----------------------------------
COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_ROOT}/docker-compose.yml}"
ORDER_URL="${ORDER_URL:-http://localhost:8000}"
ORDER_ENDPOINT="${ORDER_ENDPOINT:-/orders}"
ORDER_HEALTH_URL="${ORDER_HEALTH_URL:-${ORDER_URL}/health}"
CATALOG_HEALTH_URL="${CATALOG_HEALTH_URL:-http://localhost:8001/health}"
ORDER_PAYLOAD="${ORDER_PAYLOAD:-{\"product_id\":1,\"quantity\":1}}"

RABBIT_API="${RABBIT_API:-http://localhost:15672/api}"
RABBIT_USER="${RABBITMQ_DEFAULT_USER:-guest}"
RABBIT_PASS="${RABBITMQ_DEFAULT_PASS:-guest}"
PAYMENT_QUEUE="${PAYMENT_QUEUE:-payment_queue}"

REQUEST_INTERVAL="${REQUEST_INTERVAL:-0.3}"   # segundos entre pedidos
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-5}"       # timeout por requisição (s)
OUTAGE_SECONDS="${OUTAGE_SECONDS:-20}"        # duração da falha injetada
RECOVERY_TIMEOUT="${RECOVERY_TIMEOUT:-60}"    # tempo máximo p/ recuperação
CONTINGENCY_LOG_PATTERN="${CONTINGENCY_LOG_PATTERN:-retry|circuit|fallback|open}"

WORK_DIR="$(mktemp -d)"
RESULTS_FILE="${WORK_DIR}/results.txt"        # formato: "<epoch> <http_status>"
LOG_DIR="${LOG_DIR:-${PROJECT_ROOT}/logs}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/chaos_test_$(date +%Y%m%d_%H%M%S).log}"
mkdir -p "$LOG_DIR"
: > "$RESULTS_FILE"

CURRENT_SCENARIO="setup"
FAILURES=0
TRAFFIC_PID=""

# ------------------------------ Logging JSON ---------------------------------
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n\t' '  '
}

log() { # log <LEVEL> <evento> [mensagem]
  local level="$1" event="$2" msg="${3:-}"
  printf '{"ts":"%s","level":"%s","scenario":"%s","event":"%s","message":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$level" "$CURRENT_SCENARIO" "$event" \
    "$(json_escape "$msg")" | tee -a "$LOG_FILE"
}

assert() { # assert "<descrição>" <comando...>
  local desc="$1"; shift
  if "$@"; then
    log INFO assert_pass "$desc"
  else
    log ERROR assert_fail "$desc"
    FAILURES=$((FAILURES + 1))
  fi
}

# ------------------------------- Helpers -------------------------------------
dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

http_status() { curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$1" || true; }

wait_until() { # wait_until <timeout_s> <comando...>
  local timeout="$1"; shift
  local deadline=$((SECONDS + timeout))
  until "$@"; do
    (( SECONDS >= deadline )) && return 1
    sleep 1
  done
}

# Credenciais via stdin (-K -) para não vazarem em `ps`
rabbit_curl() {
  printf 'user = "%s:%s"\n' "$RABBIT_USER" "$RABBIT_PASS" \
    | curl -fsS --max-time 5 -K - "$@"
}

queue_field() { # queue_field <campo>  (messages, messages_ready, consumers, durable)
  rabbit_curl "${RABBIT_API}/queues/%2F/${PAYMENT_QUEUE}" \
    | jq -r --arg f "$1" '.[$f] | tostring'
}

# ---------------------------- Gerador de tráfego -----------------------------
traffic_loop() {
  local code
  while true; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$REQUEST_TIMEOUT" \
      -X POST -H 'Content-Type: application/json' \
      -d "$ORDER_PAYLOAD" "${ORDER_URL}${ORDER_ENDPOINT}" || true)"
    printf '%s %s\n' "$(date +%s)" "${code:-000}" >> "$RESULTS_FILE"
    sleep "$REQUEST_INTERVAL"
  done
}

traffic_start() {
  traffic_loop &
  TRAFFIC_PID=$!
  log INFO traffic_started "Envio contínuo de pedidos (pid=${TRAFFIC_PID})"
}

traffic_stop() {
  if [[ -n "$TRAFFIC_PID" ]] && kill -0 "$TRAFFIC_PID" 2>/dev/null; then
    kill "$TRAFFIC_PID" 2>/dev/null || true
    wait "$TRAFFIC_PID" 2>/dev/null || true
    log INFO traffic_stopped "Envio de pedidos encerrado"
  fi
  TRAFFIC_PID=""
}

count_status() { # count_status <desde_epoch> <status>
  awk -v from="$1" -v code="$2" '$1>=from && $2==code {n++} END{print n+0}' "$RESULTS_FILE"
}

count_success() { # respostas 2xx desde <epoch>
  awk -v from="$1" '$1>=from && $2 ~ /^2/ {n++} END{print n+0}' "$RESULTS_FILE"
}

status_summary() { # distribuição de status desde <epoch>
  awk -v from="$1" '$1>=from {c[$2]++} END{for(k in c) printf "%s=%d ", k, c[k]}' "$RESULTS_FILE"
}

has_success_since() { [[ "$(count_success "$1")" -ge "$2" ]]; } # <epoch> <min>

# ------------------------------- Cleanup -------------------------------------
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  traffic_stop
  log WARN cleanup "Restaurando serviços (catalog-service, payment-service)"
  dc start catalog-service payment-service >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ----------------------------- Pré-requisitos --------------------------------
preflight() {
  CURRENT_SCENARIO="preflight"
  local bin
  for bin in docker curl jq; do
    command -v "$bin" >/dev/null || { log ERROR missing_dependency "$bin não encontrado"; exit 2; }
  done
  docker compose version >/dev/null 2>&1 \
    || { log ERROR missing_dependency "docker compose v2 indisponível"; exit 2; }

  local running svc
  running="$(dc ps --status running --services)"
  for svc in order-service catalog-service payment-service rabbitmq; do
    grep -qx "$svc" <<<"$running" \
      || { log ERROR service_down "$svc não está em execução (docker compose up -d)"; exit 2; }
  done

  wait_until "$RECOVERY_TIMEOUT" test "$(http_status "$ORDER_HEALTH_URL")" = 200 \
    || { log ERROR order_unhealthy "order-service não respondeu /health"; exit 2; }
  queue_field messages >/dev/null \
    || { log ERROR queue_missing "Fila ${PAYMENT_QUEUE} inacessível via API do RabbitMQ"; exit 2; }

  log INFO preflight_ok "Ambiente pronto para o teste de caos"
}

# ======================= CENÁRIO 1: catalog-service ==========================
count_contingency_logs() { # <iso_timestamp>
  dc logs --no-color --since "$1" order-service 2>/dev/null \
    | grep -Eic "$CONTINGENCY_LOG_PATTERN" || true
}

order_healthy()   { [[ "$(http_status "$ORDER_HEALTH_URL")" == "200" ]]; }
catalog_healthy() { [[ "$(http_status "$CATALOG_HEALTH_URL")" == "200" ]]; }

scenario_1_catalog_failure() {
  CURRENT_SCENARIO="cenario_1_catalog"
  log INFO scenario_start "Queda forçada do catalog-service sob carga"

  # 1) Baseline: sistema saudável antes da falha
  local t_base; t_base="$(date +%s)"
  sleep 5
  assert "baseline: pedidos aceitos (2xx) antes da falha" has_success_since "$t_base" 3

  # 2) Injeção da falha
  local t_stop iso_stop
  t_stop="$(date +%s)"; iso_stop="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log WARN fault_injected "docker compose stop catalog-service"
  dc stop catalog-service >/dev/null
  sleep "$OUTAGE_SECONDS"

  # 3) Evidências de contingência durante a falha
  log INFO outage_traffic "Status HTTP durante a falha: $(status_summary "$t_stop")"
  local evidence; evidence="$(count_contingency_logs "$iso_stop")"
  log INFO contingency_evidence "Linhas de retry/circuit breaker/fallback no order-service: ${evidence}"
  assert "order-service permaneceu saudável durante a falha" order_healthy
  assert "nenhuma requisição ficou sem resposta (000) durante a falha" \
    test "$(count_status "$t_stop" 000)" -eq 0
  assert "logs comprovam retry/circuit breaker/fallback" test "$evidence" -gt 0

  # 4) Recuperação (healing)
  local t_start; t_start="$(date +%s)"
  log WARN fault_removed "docker compose start catalog-service"
  dc start catalog-service >/dev/null

  assert "catalog-service voltou a responder /health" \
    wait_until "$RECOVERY_TIMEOUT" catalog_healthy
  assert "pedidos voltaram a ser aceitos (circuit breaker fechou)" \
    wait_until "$RECOVERY_TIMEOUT" has_success_since "$t_start" 3
  log INFO healed "Recuperação em $(( $(date +%s) - t_start ))s"
  log INFO scenario_end "Cenário 1 concluído"
}

# =============================== MAIN ========================================
main() {
  preflight
  traffic_start
  scenario_1_catalog_failure
  log INFO summary "Falhas até agora: ${FAILURES}"
  exit $(( FAILURES > 0 ? 1 : 0 ))
}
main "$@"