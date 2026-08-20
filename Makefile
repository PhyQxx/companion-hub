.PHONY: install lint test schemas types contracts migrate compose-check infra-up infra-down llm-check p5-backend p5-frontend p5-real p5-real-l1 p5-real-l2 run

P5_REPORT ?= /tmp/aria-p5-real-model-report.json
P5_L1_REPORT ?= /tmp/aria-p5-real-model-l1-report.json
P5_L2_REPORT ?= /tmp/aria-p5-real-model-l2-report.json

install:
	uv sync --dev

lint:
	uv run ruff check server
	uv run mypy server/app server/tests

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

llm-check:
	uv run python server/scripts/check_llm.py

p5-backend:
	uv run ruff check server
	uv run mypy server/app server/tests
	uv run pytest
	git diff --check
	uv run alembic heads

p5-frontend:
	pnpm --dir web typecheck
	pnpm --dir web build

p5-real:
	uv run python server/scripts/p5_real_model_regression.py --env-file .env.local --config config/hub.example.yaml --report $(P5_REPORT)

p5-real-l1:
	uv run python server/scripts/p5_real_model_regression.py --env-file .env.local --config config/hub.example.yaml --exclude-case l2-isolation --report $(P5_L1_REPORT)

p5-real-l2:
	uv run python server/scripts/p5_real_model_regression.py --env-file .env.local --config config/hub.example.yaml --case l2-isolation --report $(P5_L2_REPORT)

run:
	uv run uvicorn app.main:app --app-dir server --reload
