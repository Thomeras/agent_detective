"""S26: property test — random DAG with an injected faulty node and decayed
descendant scores; find_blame must identify the injected culprit.

Generation is tuned so the property is meaningful: exactly one node is faulty
(below threshold with a significant drop from healthy predecessors), its
descendants decay (possibly below threshold, but always shadowed by the
fault), and every other node is healthy. Detection is therefore guaranteed,
so the property asserts it holds universally. Seeded deterministically via
derandomize=True.
"""

import networkx as nx
from hypothesis import given, settings, strategies as st

from blame_engine import BlameInput, NodeScore, find_blame


def _score(run_id: str, value: float) -> NodeScore:
    return NodeScore(
        run_id=run_id,
        score=value,
        components={},
        input_flawed=None,
        unscored_reason=None,
        judge_note=None,
    )


@settings(max_examples=100, derandomize=True, deadline=None)
@given(st.data())
def test_random_dag_with_injected_fault_finds_culprit(data: st.DataObject) -> None:
    n = data.draw(st.integers(min_value=5, max_value=12))
    nodes = [f"n{i}" for i in range(n)]

    # DAG by construction: edges only from lower to higher index.
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    rolls = data.draw(st.lists(st.integers(0, 9), min_size=len(pairs), max_size=len(pairs)))
    edges = [(nodes[i], nodes[j]) for (i, j), roll in zip(pairs, rolls) if roll < 3]

    graph = nx.DiGraph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from(edges)

    fault_idx = data.draw(st.integers(0, n - 1))
    fault = nodes[fault_idx]
    fault_score = data.draw(st.floats(min_value=0.0, max_value=0.3))
    descendants = nx.descendants(graph, fault)

    scores: dict[str, NodeScore] = {}
    for node in nodes:
        if node == fault:
            scores[node] = _score(node, fault_score)
        elif node in descendants:
            # Decayed descendant: INHERITS the degradation — stays between the
            # fault score and just below the blame threshold, never recovering
            # above it. (If a descendant were allowed to recover past threshold
            # and a grandchild then re-broke from that healthy node, the engine
            # would correctly report TWO independent origins — a legitimate
            # multi_culprit, not the single-fault invariant this test asserts.
            # That recovery topology is covered by the dedicated edge-drop tests.)
            delta = data.draw(st.floats(min_value=0.0, max_value=0.4))
            scores[node] = _score(node, min(0.49, fault_score + delta))
        else:
            # Ancestors and unrelated nodes stay healthy.
            scores[node] = _score(node, data.draw(st.floats(min_value=0.8, max_value=1.0)))

    inp = BlameInput(
        nodes=nodes,
        edges=edges,
        scores=scores,
        node_costs={node: 1.0 for node in nodes},
        node_end_times={node: float(i) for i, node in enumerate(nodes)},
        agent_names={node: node for node in nodes},
        error_span_ids={},
        terminal_verdict=None,
        loop_baselines={},
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == [fault]
