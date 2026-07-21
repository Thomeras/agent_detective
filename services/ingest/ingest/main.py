"""Ingest service entrypoint.

TODO(M3): implement the FastAPI app: POST /v1/traces (OTLP/HTTP JSON),
raw spans to ClickHouse, otel_mapper to Postgres graphs/runs/edges,
payloads inline/MinIO, finalizer task emitting ad.graphs.completed,
plus a health endpoint. See build spec section 6.2.
"""
