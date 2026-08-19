.DEFAULT_GOAL := help

.PHONY: help dev sync sync-dev test lint lint-fix format typecheck check clean patch minor

help:
	@echo "Доступные команды:"
	@echo "  make dev          — запустить dev-сервер (uvicorn --reload, порт 8000 по умолчанию)"
	@echo "  make dev 8001     — запустить dev-сервер на порту 8001"
	@echo "  make sync         — установить зависимости (uv sync)"
	@echo "  make sync-dev     — установить зависимости с --extra dev"
	@echo "  make test         — запустить тесты"
	@echo "  make patch        — bump patch-версию"
	@echo "  make minor        — bump minor-версию"
	@echo "  make lint         — проверить код ruff"
	@echo "  make lint-fix     — исправить lint-ошибки ruff --fix"
	@echo "  make format       — отформатировать код ruff format"
	@echo "  make typecheck    — проверить типы mypy"
	@echo "  make check        — lint + typecheck"
	@echo "  make clean        — удалить __pycache__, .ruff_cache, .pytest_cache, .mypy_cache"

PORT_ARGS := $(filter-out dev,$(MAKECMDGOALS))
PORT := $(if $(PORT_ARGS),$(PORT_ARGS),8000)
ENV_FILE_ARG := $(if $(wildcard .env.dev),--env-file .env.dev,)
dev:
	(fuser -k $(PORT)/tcp 2>/dev/null || true) && MICRO_MODE=dev uv run uvicorn main:app --reload --port $(PORT) $(ENV_FILE_ARG)
$(PORT_ARGS):
	@:

sync:
	uv sync

sync-dev:
	uv sync --extra dev

test:
	uv run pytest

patch:
	uv version --bump patch
	
minor:
	uv version --bump minor
	
lint:
	uv run ruff check .

lint-fix:
	uv run ruff check . --fix

format:
	uv run ruff format .

typecheck:
	uv run mypy micro/

check: lint typecheck

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true