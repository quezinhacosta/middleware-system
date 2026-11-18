# Dockerfile multi-stage parametrizado: gera UMA imagem por microsservico.
#
# Uso:
#   docker build --build-arg SERVICE=order_service --build-arg PORT=8000 \
#       -t middleware-system/order-service:latest .
#
# Cada imagem contem apenas o middleware compartilhado e o codigo do servico
# indicado em SERVICE (nome do pacote dentro de services/).


# ---------- Estagio 1: Builder (dependencias Python) ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /install

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- Estagio 2: Runtime (minimo e seguro) ----------
FROM python:3.11-slim AS runtime

# Pacote do servico dentro de services/ (ex.: order_service) - obrigatorio
ARG SERVICE
# Porta HTTP padrao do servico (pode ser sobrescrita em runtime via env PORT)
ARG PORT=8000

RUN test -n "${SERVICE}" || (echo "ERRO: informe --build-arg SERVICE=<nome_do_servico>" && exit 1)

# PYTHONPATH inclui a pasta do servico para imports locais (ex.: payment usa "from src...")
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/services/${SERVICE} \
    SERVICE_MODULE=services.${SERVICE}.main \
    PORT=${PORT}

WORKDIR /app

# Copia pacotes Python pre-instalados do builder
COPY --from=builder /install /usr/local

# Copia o middleware compartilhado e somente o servico alvo
COPY middleware/ /app/middleware/
COPY services/__init__.py /app/services/__init__.py
COPY services/${SERVICE}/ /app/services/${SERVICE}/

# Usuario sem privilegios de root (hardening); /data guarda estado persistente
RUN useradd -m -u 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE ${PORT}

# Healthchecks ficam no docker-compose (permite sobrescrever o comando nos testes)
CMD ["sh", "-c", "exec uvicorn ${SERVICE_MODULE}:app --host 0.0.0.0 --port ${PORT}"]
