"""Graph helpers shared by tier1 and tier2.

Turns a ``GraphBundle`` (rows from Postgres) into the structures the blame
engine consumes: terminal/root run detection and ``BlameInput`` construction.
Kept dependency-light (only blame_engine types) so it is exercised directly in
tests.
"""

from __future__ import annotations

from datetime import datetime

from blame_engine import BlameConfig, BlameInput, LoopBaseline, NodeScore, TerminalVerdict

from .types import AgentStat, GraphBundle, RunRecord


def to_epoch(value: datetime | None) -> float:
    """Epoch seconds for a timestamp; 0.0 when missing (sorts earliest)."""
    return value.timestamp() if value is not None else 0.0


def terminal_runs(bundle: GraphBundle) -> list[RunRecord]:
    """Runs with no outgoing edge (sinks), latest end_time first.

    Falls back to the single latest-finished run when the graph has no edges.
    """
    has_outgoing = {e.from_run_id for e in bundle.edges}
    sinks = [r for r in bundle.runs if r.run_id not in has_outgoing]
    if not sinks:
        sinks = list(bundle.runs)
    return sorted(sinks, key=lambda r: (to_epoch(r.ended_at), str(r.run_id)), reverse=True)


def root_runs(bundle: GraphBundle) -> list[RunRecord]:
    """Runs with no incoming edge (sources), earliest start_time first."""
    has_incoming = {e.to_run_id for e in bundle.edges}
    roots = [r for r in bundle.runs if r.run_id not in has_incoming]
    if not roots:
        roots = list(bundle.runs)
    return sorted(roots, key=lambda r: (to_epoch(r.started_at), str(r.run_id)))


def terminal_run(bundle: GraphBundle) -> RunRecord | None:
    """The single terminal (final) run whose output represents the graph."""
    runs = terminal_runs(bundle)
    return runs[0] if runs else None


# Verifier/gate node name hints (mirrors blame_engine._VERIFIER_HINTS): a node
# whose job is to PASS/FAIL work. Its OUTPUT is a verdict, not the deliverable.
_VERIFIER_HINTS = (
    "qa", "eval", "review", "verif", "validat", "check", "critic", "audit", "gate"
)


def _is_verifier(name: str | None) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _VERIFIER_HINTS)


def _has_output(run: RunRecord) -> bool:
    return run.output_inline is not None or run.output_overflow_ref is not None


def deliverable_run(bundle: GraphBundle) -> RunRecord | None:
    """The run whose OUTPUT is the graph's actual deliverable — what the terminal
    judge must grade.

    This is deliberately NOT ``terminal_run``. Two traps it avoids:

    - **Retry loops hide the sink.** A back-edge (e.g. ``eval -> act`` on a QA
      retry) leaves every node with an outgoing edge, so ``terminal_runs`` finds
      no sink and falls back to the latest-ended run — which is the orchestrator
      ROOT wrapping the whole run, and the root has no output of its own.
      Grading that empty output produces a fabricated "final output is empty"
      verdict on a perfectly healthy run. So when there is no true sink we fall
      back to producing, non-source runs only — never the root.
    - **A verifier sink is not the artifact.** A ``qa``/``eval`` sink emits a
      PASS/FAIL verdict, not the deliverable. Walk back to the nearest
      non-verifier producer whose output carries the real artifact.
    """
    has_outgoing = {e.from_run_id for e in bundle.edges}
    has_incoming = {e.to_run_id for e in bundle.edges}
    sinks = [r for r in bundle.runs if r.run_id not in has_outgoing]
    if not sinks:
        # Cyclic (retry loop): no true sink. Restrict to runs that actually
        # produced output and are not pure sources — this excludes the
        # orchestrator root, which is the whole point.
        sinks = [
            r for r in bundle.runs if _has_output(r) and r.run_id in has_incoming
        ] or list(bundle.runs)
    term = max(sinks, key=lambda r: (to_epoch(r.ended_at), str(r.run_id)))

    preds_of: dict[object, list[object]] = {}
    for e in bundle.edges:
        preds_of.setdefault(e.to_run_id, []).append(e.from_run_id)
    by_id = {r.run_id: r for r in bundle.runs}
    node, seen = term, set()
    while _is_verifier(node.agent_name) and node.run_id not in seen:
        seen.add(node.run_id)
        preds = [by_id[p] for p in preds_of.get(node.run_id, []) if p in by_id]
        producers = [p for p in preds if not _is_verifier(p.agent_name) and _has_output(p)]
        if producers:
            node = max(producers, key=lambda r: (to_epoch(r.ended_at), str(r.run_id)))
            break
        # Only verifier predecessors (e.g. qa -> eval): step through the latest
        # one and keep walking up until a real producer appears.
        verifier_preds = [p for p in preds if p.run_id not in seen]
        if not verifier_preds:
            break  # nothing but verifiers all the way up: grade the verdict
        node = max(verifier_preds, key=lambda r: (to_epoch(r.ended_at), str(r.run_id)))
    return node


def root_run(bundle: GraphBundle) -> RunRecord | None:
    roots = root_runs(bundle)
    return roots[0] if roots else None


def build_config(
    *,
    threshold: float,
    gap_threshold: float,
    min_drop: float,
    max_loop_iterations: int,
    cum_drop_threshold: float = 0.30,
    cum_min_edges: int = 2,
    cum_step_min: float = 0.05,
) -> BlameConfig:
    return BlameConfig(
        threshold=threshold,
        gap_threshold=gap_threshold,
        min_drop=min_drop,
        max_loop_iterations=max_loop_iterations,
        cum_drop_threshold=cum_drop_threshold,
        cum_min_edges=cum_min_edges,
        cum_step_min=cum_step_min,
    )


def _loop_baselines(baselines: dict[str, AgentStat]) -> dict[str, LoopBaseline]:
    out: dict[str, LoopBaseline] = {}
    for agent_name, stat in baselines.items():
        if stat.iterations_mean is None or stat.sample_count is None:
            continue
        out[agent_name] = LoopBaseline(
            mean_iterations=float(stat.iterations_mean),
            std_iterations=float(stat.iterations_std or 0.0),
            sample_count=int(stat.sample_count),
        )
    return out


def _common_fields(bundle: GraphBundle) -> dict[str, object]:
    nodes = [str(r.run_id) for r in bundle.runs]
    edges = [(str(e.from_run_id), str(e.to_run_id)) for e in bundle.edges]
    node_costs = {str(r.run_id): (r.cost_usd or 0.0) for r in bundle.runs}
    node_end_times = {str(r.run_id): to_epoch(r.ended_at) for r in bundle.runs}
    agent_names = {str(r.run_id): (r.agent_name or str(r.run_id)) for r in bundle.runs}
    error_span_ids = {str(r.run_id): [] for r in bundle.runs}
    return {
        "nodes": nodes,
        "edges": edges,
        "node_costs": node_costs,
        "node_end_times": node_end_times,
        "agent_names": agent_names,
        "error_span_ids": error_span_ids,
    }


def build_loop_input(
    bundle: GraphBundle,
    baselines: dict[str, AgentStat],
    config: BlameConfig,
) -> BlameInput:
    """A minimal BlameInput (empty scores) for the tier1 loop quick-check."""
    fields = _common_fields(bundle)
    return BlameInput(
        nodes=fields["nodes"],
        edges=fields["edges"],
        scores={},
        node_costs=fields["node_costs"],
        node_end_times=fields["node_end_times"],
        agent_names=fields["agent_names"],
        error_span_ids=fields["error_span_ids"],
        terminal_verdict=None,
        loop_baselines=_loop_baselines(baselines),
        config=config,
    )


def build_blame_input(
    bundle: GraphBundle,
    scores: dict[str, NodeScore],
    terminal_verdict: TerminalVerdict | None,
    baselines: dict[str, AgentStat],
    config: BlameConfig,
) -> BlameInput:
    """The full BlameInput for tier2 scoring + blame."""
    fields = _common_fields(bundle)
    return BlameInput(
        nodes=fields["nodes"],
        edges=fields["edges"],
        scores=scores,
        node_costs=fields["node_costs"],
        node_end_times=fields["node_end_times"],
        agent_names=fields["agent_names"],
        error_span_ids=fields["error_span_ids"],
        terminal_verdict=terminal_verdict,
        loop_baselines=_loop_baselines(baselines),
        config=config,
    )
