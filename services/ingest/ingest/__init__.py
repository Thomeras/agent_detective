"""OTLP/HTTP trace ingest service (build spec section 6.2, milestone M3).

Receives OTLP trace exports, stores raw spans in ClickHouse, reconstructs
execution graphs into Postgres via otel_mapper, routes large payloads to
object storage, and finalizes quiesced graphs onto the ad.graphs.completed
Redis stream for the worker pipeline.
"""
