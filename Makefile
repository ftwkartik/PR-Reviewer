.PHONY: install lint fmt type test up migrate eval-retrieval eval
install:; uv sync
lint:; uv run ruff check . && uv run ruff format --check .
fmt:; uv run ruff check --fix . && uv run ruff format .
type:; uv run mypy app
test:; uv run pytest
up:; docker compose up --build
migrate:; uv run alembic upgrade head
eval-retrieval:; uv run python -m evals.run_eval --retrieval-only
eval:; uv run python -m evals.run_eval
