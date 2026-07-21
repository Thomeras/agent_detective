"""Shared BlameInput factory for behavior tests."""

from collections.abc import Mapping, Sequence

import pytest

from blame_engine import BlameConfig, BlameInput, NodeScore


def _wrap_score(run_id: str, value: float | None | NodeScore) -> NodeScore:
    if isinstance(value, NodeScore):
        return value
    return NodeScore(
        run_id=run_id,
        score=value,
        components={},
        input_flawed=None,
        unscored_reason="payload_missing" if value is None else None,
        judge_note=None,
    )


def make_input(
    *,
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]] = (),
    scores: Mapping[str, float | None | NodeScore] | None = None,
    costs: Mapping[str, float] | None = None,
    end_times: Mapping[str, float] | None = None,
    agent_names: Mapping[str, str] | None = None,
    error_span_ids: Mapping[str, list[str]] | None = None,
    terminal_verdict=None,
    loop_baselines=None,
    config: BlameConfig | None = None,
) -> BlameInput:
    """Build a BlameInput with deterministic defaults.

    scores maps run_id -> float score, None (unknown), or a full NodeScore.
    Default end_times follow node order (0.0, 1.0, ...), costs default to 1.0,
    agent_names default to the run_id itself.
    """
    node_list = list(nodes)
    raw_scores = scores or {}
    return BlameInput(
        nodes=node_list,
        edges=list(edges),
        scores={n: _wrap_score(n, raw_scores.get(n)) for n in node_list},
        node_costs={n: (costs or {}).get(n, 1.0) for n in node_list},
        node_end_times={
            n: (end_times or {}).get(n, float(i)) for i, n in enumerate(node_list)
        },
        agent_names={n: (agent_names or {}).get(n, n) for n in node_list},
        error_span_ids=dict(error_span_ids or {}),
        terminal_verdict=terminal_verdict,
        loop_baselines=dict(loop_baselines or {}),
        config=config or BlameConfig(),
    )


@pytest.fixture
def mk():
    return make_input
