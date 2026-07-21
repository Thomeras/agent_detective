# Ingest service image. Build context is the repo root (uv workspace).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Workspace members must all be present for uv sync to resolve.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# TODO(M3): replace this placeholder with the real server:
#   CMD ["uvicorn", "ingest.main:app", "--host", "0.0.0.0", "--port", "8001"]
CMD ["python", "-m", "http.server", "8001"]
