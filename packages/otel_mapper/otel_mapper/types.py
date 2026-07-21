"""Public data types for otel_mapper (Apache-2.0).

Pure data, no logic beyond defaults. All types are immutable dataclasses so
callers (ingest in M3) can rely on them not changing underfoot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class EdgeType(str, Enum):
    """Edge types matching the ``edges.type`` CHECK constraint (build spec 5)."""

    SPAWN = "SPAWN"
    A2A_MESSAGE = "A2A_MESSAGE"
    TOOL_DELEGATION = "TOOL_DELEGATION"


@dataclass(frozen=True)
class AgentRunCandidate:
    """One reconstructed agent run, keyed deterministically by ``run_key``.

    ``run_key`` is ``"<trace_id>:<span_id>"`` of the AGENT span that opened the
    run. It is stable across redelivery of the same spans; ingest is expected
    to hash it into the ``agent_runs.run_id`` UUID (e.g. uuid5).
    """

    run_key: str
    graph_id: str
    trace_id: str
    agent_name: str | None
    agent_version: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    input: str | None
    output: str | None
    start_time: datetime | None
    end_time: datetime | None
    status: str  # "ok" | "failed"; "degraded" is a downstream quality judgement


@dataclass(frozen=True)
class EdgeCandidate:
    """A directed edge between two runs, pointing in the direction of influence.

    ``detection_method`` is free text recording which detection rule fired.
    """

    from_run_key: str
    to_run_key: str
    type: EdgeType
    detection_method: str


@dataclass(frozen=True)
class MappingResult:
    """Output of :func:`otel_mapper.map_spans`.

    ``runs`` is sorted by (start_time, run_key); ``edges`` by
    (from_run_key, to_run_key, type); both orders are deterministic for a
    given input. ``graph_ids`` is the set of graph identities seen (correlation
    header values, or trace ids when no header is present).
    """

    runs: list[AgentRunCandidate] = field(default_factory=list)
    edges: list[EdgeCandidate] = field(default_factory=list)
    graph_ids: set[str] = field(default_factory=set)
