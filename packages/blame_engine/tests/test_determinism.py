"""The report may not depend on the ORDER the input was written down in.

``nodes`` and ``edges`` are sets in meaning and lists in representation. Nothing
about a run changes when the exporter flushes its spans in a different order, so
nothing about the verdict may either — and everything downstream quietly assumes
it: golden fixtures, judge cassettes keyed on a recorded report, the A/B/C
corpus replay, cross-run diffs, and the README's determinism claim.

Both regressions below were found by the topological fuzzer
(``test_property.py::test_report_is_invariant_under_input_permutation``) and have
the same root cause: code that ordered work by ``nx.condensation``'s component
IDs. Those IDs are handed out in the order components are discovered, which
follows the order edges were inserted — they are not a stable identity for a
node. The rest of the engine orders by the exit node's chronological key
(``_chron_key``), and these two places now do too.

The fuzzer covers this space at random; these fixtures pin the two concrete
shapes so a reintroduction fails with a readable name instead of a seed.
"""

import pytest

from blame_engine import find_blame

# Both shapes need a TIE for the bug to show — with distinct scores the "best"
# successor is unique and either ordering finds it.
_DIAMOND_EDGES = [("n0", "n1"), ("n0", "n2"), ("n1", "n3"), ("n2", "n3")]
_TIE_EDGES = [("n0", "n1"), ("n0", "n2"), ("n1", "n3")]


def _permutations(edges: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """A few orderings of the same edge set, including the reverse."""
    return [
        list(edges),
        list(reversed(edges)),
        [edges[i] for i in (1, 0, 3, 2)] if len(edges) == 4 else [edges[i] for i in (1, 2, 0)],
    ]


def test_diamond_propagation_path_does_not_depend_on_edge_order(mk) -> None:
    """One culprit, two branches, one join: two paths of EQUAL length.

    ``nx.shortest_path`` returned whichever its traversal happened to reach
    first, so the same run reported the failure travelling through n1 or through
    n2 depending on span order.
    """
    scores = {"n0": 0.0, "n1": 0.4, "n2": 0.2, "n3": 0.1}
    paths = {
        tuple(
            find_blame(
                mk(nodes=["n0", "n1", "n2", "n3"], edges=edges, scores=scores)
            ).propagation_path
        )
        for edges in _permutations(_DIAMOND_EDGES)
    }
    assert len(paths) == 1, f"propagation path depends on edge order: {paths}"
    # The tiebreak is not arbitrary: among equally short paths the report shows
    # the one through the MORE degraded branch — the path it claims to describe.
    assert paths.pop() == ("n0", "n2", "n3")


def test_degradation_chain_does_not_depend_on_edge_order(mk) -> None:
    """n0 (1.00) has two successors tied at 0.50. Only one of them continues
    into a second declining edge, so which successor the chain walk picks
    decides whether a degradation chain is reported at all — and with it the
    candidacy line every node on the chain gets."""
    scores = {"n0": 1.0, "n1": 0.5, "n2": 0.5, "n3": 0.0}
    seen = set()
    for edges in _permutations(_TIE_EDGES):
        report = find_blame(
            mk(nodes=["n0", "n1", "n2", "n3"], edges=edges, scores=scores)
        )
        seen.add(
            (
                tuple(tuple(p["path"]) for p in report.evidence.degradation_paths),
                repr(report.evidence.candidacy_records["n0"]),
                report.report_type,
            )
        )
    assert len(seen) == 1, f"degradation chain depends on edge order: {seen}"


@pytest.mark.parametrize(
    "edges,scores",
    [
        (_DIAMOND_EDGES, {"n0": 0.0, "n1": 0.4, "n2": 0.2, "n3": 0.1}),
        (_TIE_EDGES, {"n0": 1.0, "n1": 0.5, "n2": 0.5, "n3": 0.0}),
        # A cycle: component discovery order is what assigns the ids.
        ([("n0", "n1"), ("n1", "n2"), ("n2", "n1"), ("n1", "n3")],
         {"n0": 0.95, "n1": 0.2, "n2": 0.3, "n3": 0.25}),
    ],
)
def test_whole_verdict_is_stable_under_edge_permutation(mk, edges, scores) -> None:
    """The surface a stored report is compared on: type, culprits, confidence,
    path, cost and the full candidacy trace."""
    nodes = ["n0", "n1", "n2", "n3"]
    reports = [
        find_blame(mk(nodes=nodes, edges=perm, scores=scores))
        for perm in _permutations(edges)
    ]
    first = reports[0]
    for other in reports[1:]:
        assert other.report_type == first.report_type
        assert other.culprit_run_ids == first.culprit_run_ids
        assert other.confidence == pytest.approx(first.confidence)
        assert other.propagation_path == first.propagation_path
        assert other.downstream_cost_usd == pytest.approx(first.downstream_cost_usd)
        assert other.evidence.candidacy_records == first.evidence.candidacy_records
        assert other.evidence.note_records == first.evidence.note_records
        assert other.evidence.topo_order == first.evidence.topo_order
