"""Algorithm 2: loop anomaly detection (spec 3.4).

A super-node with iterations > 1 (a self-loop counts as two iterations) is
anomalous when it exceeds max_loop_iterations, or when a baseline of its
dominant agent_name with enough history exists and the iteration count is a
statistical outlier. Benign loops produce nothing.
"""

from collections import Counter

from .condense import Condensation, SuperNode, condense
from .types import BlameInput, LoopAnomaly, LoopBaseline


def _dominant_agent_name(inp: BlameInput, sn: SuperNode) -> str:
    """Most common agent_name among members; ties broken alphabetically."""
    names = [inp.agent_names.get(m, m) for m in sn.members]
    counts = Counter(names)
    return min(counts, key=lambda n: (-counts[n], n))


def _is_statistical_outlier(iterations: int, baseline: LoopBaseline | None, inp: BlameInput) -> bool:
    cfg = inp.config
    return (
        baseline is not None
        and baseline.sample_count >= cfg.loop_min_history
        and iterations > baseline.mean_iterations + cfg.loop_zscore * baseline.std_iterations
    )


def _detect_anomalies(cond: Condensation, inp: BlameInput) -> list[LoopAnomaly]:
    cfg = inp.config
    anomalies: list[LoopAnomaly] = []
    for sid in cond.topo:
        sn = cond.super_nodes[sid]
        if sn.iterations == 1 and not sn.has_self_loop:
            continue
        # A self-loop is a single node retrying itself: counts as 2 iterations.
        iterations = sn.iterations if sn.iterations > 1 else 2
        baseline = inp.loop_baselines.get(_dominant_agent_name(inp, sn))
        if iterations > cfg.max_loop_iterations:
            kind, used_baseline = "max_iterations", None
        elif _is_statistical_outlier(iterations, baseline, inp):
            kind, used_baseline = "statistical", baseline
        else:
            continue
        anomalies.append(
            LoopAnomaly(
                member_run_ids=list(sn.members),
                agent_names=[inp.agent_names.get(m, m) for m in sn.members],
                iterations=iterations,
                limit_kind=kind,
                baseline=used_baseline,
            )
        )
    return anomalies


def detect_loop_anomalies(inp: BlameInput) -> list[LoopAnomaly]:
    """All anomalous loops in deterministic topological order."""
    return _detect_anomalies(condense(inp), inp)
