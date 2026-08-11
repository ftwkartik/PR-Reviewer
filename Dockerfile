FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /srv
COPY --from=builder /srv/.venv /srv/.venv
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
