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
    model_name: str | None
    prompt_hash: str | None
    tool_schema_hash: str | None  # agent_detective.tool_schema_hash identity attribute
    artifact_meta: str | None  # raw agent_detective.artifact_meta span attribute
    # Raw agent_detective.contract_params span attribute (JSON object of
    # carried parameters the run's input is contractually bound to, e.g.
    # {"file_type": "pdf"}). The convention lane into the deterministic
    # contract channel for pipelines that cannot ship JSON payloads.
    contract_params: str | None
    # Compact JSON digest of the run's TOOL member spans, in execution order:
    # [{"name": ..., "args_sha": <12 hex of sha256(input.value)>, "status":
    # "ok"|"error"}, ...]. None when the run has no TOOL member spans.
    tool_calls: str | None
    # Loop identity, from ``agent_detective.attempt`` / ``.attempt_of``. An
    # instrumentation that numbers retries has to give each attempt a DISTINCT
    # agent_name (``write#1``, ``write#2``) or reconstruction emits no edge
    # between them — which also means the reconstructed graph can no longer
    # tell "one agent that ran four times" from "four agents". These two carry
    # that back: ``attempt_of`` is the agent the attempts belong to, ``attempt``
    # is which pass this was. Both None for a run that is not a loop attempt.
    attempt: int | None
    attempt_of: str | None
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

    ``graph_types`` maps each graph id to the OTLP resource ``service.name`` of
    the runs exported under it (the cohort key ingest stores as
    ``execution_graphs.graph_type``); ``None`` when the resource carried no
    service.name. First non-empty value in deterministic run order wins.
    """

    runs: list[AgentRunCandidate] = field(default_factory=list)
    edges: list[EdgeCandidate] = field(default_factory=list)
    graph_ids: set[str] = field(default_factory=set)
    graph_types: dict[str, str | None] = field(default_factory=dict)
