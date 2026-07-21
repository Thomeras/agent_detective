# One-shot migration runner: applies `alembic upgrade head` from db/.
# Kept self-contained (pip, not the uv workspace) so it has no build
# dependency on the application packages.
FROM python:3.12-slim

WORKDIR /db

RUN pip install --no-cache-dir "alembic>=1.13" "sqlalchemy>=2.0" "psycopg2-binary>=2.9"

COPY db/ /db/

CMD ["alembic", "upgrade", "head"]
