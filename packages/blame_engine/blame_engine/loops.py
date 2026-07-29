"""Algorithm 2: loop anomaly detection (spec 3.4).

A super-node with iterations > 1 (a self-loop counts as two iterations) is
anomalous when it exceeds max_loop_iterations, or when a baseline of its
dominant agent_name with enough history exists and the iteration count is a
statistical outlier. Benign loops produce nothing.

"Iterations" means ROUNDS — how many times the loop body ran — because that is
what max_loop_iterations bounds ("burned iterations past the limit"). The SCC's
member count is not that number: a three-node body over three rounds is ten
members and three rounds, and reading size as rounds reported a bounded nested
loop (2 outer x 3 inner, nothing runaway) as 21 runaway iterations at 100%
confidence, with all 21 members named as origin. When the trace records the
real count (``agent_detective.attempt``), read it; when it does not, member
count is the only signal the older shape offers and stays the fallback.
"""

from collections import Counter, defaultdict

from .condense import Condensation, SuperNode, condense
from .types import BlameInput, LoopAnomaly, LoopBaseline


def _dominant_agent_name(inp: BlameInput, sn: SuperNode, repeating: list[str]) -> str:
    """Most common agent_name among members; ties broken alphabetically.

    An attempt's agent_name is per-attempt (``write#2``), so on an instrumented
    loop it is ``attempt_of`` that names the agent a baseline was recorded for —
    keying on the numbered name looked every baseline up under a name that can
    only ever occur once.
    """
    if repeating:
        return inp.node_attempt_of[repeating[0]]
    names = [inp.agent_names.get(m, m) for m in sn.members]
    counts = Counter(names)
    return min(counts, key=lambda n: (-counts[n], n))


def _rounds(inp: BlameInput, sn: SuperNode) -> tuple[int, list[str]]:
    """(rounds, the runs that repeated) for one cycle.

    Grouping is by ``attempt_of`` — the agent the attempts belong to — and the
    busiest group wins: a write/qa loop over four rounds is four rounds, not
    eight. Members with no loop identity (the controller, a node merely caught
    in the cycle) are not rounds of anything and are not counted.
    """
    by_agent: dict[str, list[str]] = defaultdict(list)
    for run_id in sn.members:
        agent = inp.node_attempt_of.get(run_id)
        if agent and run_id in inp.node_attempts:
            by_agent[agent].append(run_id)
    if not by_agent:
        # Nothing said how many rounds this was; cycle size is all there is.
        return len(sn.members), []
    busiest = max(by_agent, key=lambda a: (len(by_agent[a]), a))
    repeated = sorted(by_agent[busiest], key=lambda r: inp.node_attempts[r])
    return len(repeated), repeated


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
        rounds, repeating = _rounds(inp, sn)
        # A self-loop is a single node retrying itself: counts as 2 iterations.
        iterations = max(rounds, 2) if sn.has_self_loop else rounds
        baseline = inp.loop_baselines.get(_dominant_agent_name(inp, sn, repeating))
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
                repeating_run_ids=repeating,
            )
        )
    return anomalies


def detect_loop_anomalies(inp: BlameInput) -> list[LoopAnomaly]:
    """All anomalous loops in deterministic topological order."""
    return _detect_anomalies(condense(inp), inp)
