.PHONY: run worker test lint fmt docker-up docker-down migrate setup

run:
	uv run uvicorn forge.main:app --factory --reload --host 0.0.0.0 --port 8420

worker:
	uv run python -m forge.worker

test:
	uv run pytest -v

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

migrate:
	uv run alembic upgrade head

setup:
	uv run python scripts/setup_gitlab.py
