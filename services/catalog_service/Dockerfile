# ==============================================================================
# Multi-stage Dockerfile para o Catalog Service e Microsserviços
# ==============================================================================

# Estágio 1: Builder (instalação de dependências compiladas e wheels)
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

# Estágio 2: Runtime Final (mínimo e seguro)
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Instala apenas curl para healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copia pacotes python pré-instalados do builder
COPY --from=builder /install /usr/local

# Copia código-fonte da aplicação
COPY middleware/ /app/middleware/
COPY services/ /app/services/

# Cria usuário não-root (princípio de menor privilégio)
RUN useradd -m -u 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data \
    && chmod 777 /data

USER appuser

EXPOSE 8001

CMD ["uvicorn", "services.catalog_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
