FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Instala dependencias minimas do sistema e curl para healthchecks dos containers
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Instala dependencias Python com cache otimizado
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copia a camada de middleware compartilhado e todos os servicos da aplicacao
COPY middleware/ /app/middleware/
COPY services/ /app/services/

# Cria usuario sem privilegios de root por seguranca (Hardening)
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# O comando de inicializacao especifico e a porta sao definidos via docker-compose para cada servico
CMD ["python3", "-c", "print('Middleware-System Base Container')"]

