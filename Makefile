.PHONY: help install lint format test test-unit test-integration test-cov build up down logs clean pre-commit-install

# Python e ferramentas
PYTHON ?= python3
PIP ?= pip

# Cobertura mínima exigida nos testes unitários do middleware
COV_MIN ?= 60

help:
	@echo "Comandos disponíveis no Makefile:"
	@echo "  make install             Instala dependencias e configura pre-commit"
	@echo "  make pre-commit-install  Instala os git hooks do pre-commit"
	@echo "  make lint                Executa linters (Ruff) e checagem de tipos (Mypy)"
	@echo "  make format              Formata o codigo automaticamente com Ruff"
	@echo "  make test                Executa todos os testes (unitarios + integracao)"
	@echo "  make test-unit           Executa so os testes unitarios (sem Docker) com cobertura minima"
	@echo "  make test-integration    Executa os testes de integracao (sobe containers via docker compose)"
	@echo "  make test-cov            Executa os testes unitarios gerando relatorio de cobertura"
	@echo "  make build               Constroi as imagens Docker dos microsservicos"
	@echo "  make up                  Sobe a stack completa com docker compose"
	@echo "  make down                Para a stack e remove os containers"
	@echo "  make logs                Exibe os logs em tempo real dos containers"
	@echo "  make clean               Remove arquivos temporarios e caches (.pytest_cache, __pycache__)"

install:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt
	pre-commit install

pre-commit-install:
	pre-commit install

lint:
	ruff check .
	@if [ $$(find . -maxdepth 4 -name "*.py" -not -path "*/.*" | wc -l) -gt 0 ]; then \
		mypy . --ignore-missing-imports; \
	else \
		echo "Nenhum arquivo Python (.py) encontrado ainda para o Mypy."; \
	fi

format:
	ruff check --fix .
	ruff format .

test:
	$(PYTHON) -m pytest tests -v

test-unit:
	$(PYTHON) -m pytest tests -m "not integration" -v --cov=middleware --cov-report=term-missing --cov-fail-under=$(COV_MIN)

test-integration:
	$(PYTHON) -m pytest tests/integrations -m integration -v

test-cov:
	$(PYTHON) -m pytest tests -m "not integration" --cov=middleware --cov=services --cov-report=term-missing

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.py[cod]" -delete
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov