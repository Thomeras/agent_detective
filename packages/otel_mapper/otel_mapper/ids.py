"""Deterministic identity for the mapper's stable keys.

``map_spans`` keys a run by ``"<trace_id>:<span_id>"`` and a graph by its
correlation-header value (or trace id). Consumers need a UUID, and they must
all derive the SAME one: the ingest service upserts ``agent_runs.run_id`` from
it while the local-mode CLI builds an in-memory graph from it, and a blame
report that named different ids for the same span would be unreadable across
the two paths.

uuid5 over these keys makes redelivery of the same spans idempotent — the same
spans always hash to the same rows, so ON CONFLICT handling turns a replay into
a no-op.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5


def run_id_from_key(run_key: str) -> UUID:
    """Deterministic run UUID for an ``AgentRunCandidate.run_key``."""
    return uuid5(NAMESPACE_URL, run_key)


def graph_id_from_str(graph_id: str) -> UUID:
    """Deterministic graph UUID for a mapper graph id."""
    return uuid5(NAMESPACE_URL, graph_id)
