.PHONY: install lint test schemas types contracts migrate compose-check infra-up infra-down run

install:
	uv sync --dev

lint:
	uv run ruff check server
	uv run mypy server/app

test:
	uv run pytest

schemas:
	uv run python server/scripts/export_schemas.py

types: schemas
	pnpm --dir contracts install --frozen-lockfile
	pnpm --dir contracts run generate
	pnpm --dir contracts run check

contracts: schemas types test

migrate:
	uv run alembic upgrade head

compose-check:
	docker compose config --quiet

infra-up:
	docker compose up --build -d

infra-down:
	docker compose down

run:
	uv run uvicorn app.main:app --app-dir server --reload
