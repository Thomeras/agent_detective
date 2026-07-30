"""Advisory topology classification of the execution graph.

``classify_topology`` implements the frozen topology contract: a pure,
deterministic description of the graph's SHAPE (pipeline, star, mesh, ...).
The classification is presentational metadata for the UI and reports — it must
NEVER change report_type, culprits or candidacy. Two behavioral uses are
allowed, both stated here so no third can be smuggled in:

- ``disconnected`` — an instrumentation-quality note in blame.py (same family
  as payload_missing warnings);
- ``chain`` — discounts ATTRIBUTION confidence (never observation, never the
  culprit): on an unbranched line every interior node is an articulation
  point, so "the cut point is HERE" is a statement about ordering, and a
  shape with no discriminating power may not be sold as one that has some.

Tolerant by design: edges naming unknown nodes are ignored.
"""

import networkx as nx


def _build(nodes: list[str], edges: list[tuple[str, str]]) -> nx.DiGraph:
    node_set = set(nodes)
    g = nx.DiGraph()
    g.add_nodes_from(nodes)
    g.add_edges_from(
        (u, v) for u, v in edges if u in node_set and v in node_set
    )
    return g


def _is_simple_path(g: nx.DiGraph) -> bool:
    """True when the graph is a single directed simple path v1 -> ... -> vk.

    A single node (no self-edge) is a trivial simple path — this is what makes
    a whole-graph SCC condense to a 'pipeline_with_feedback' shape.
    """
    k = g.number_of_nodes()
    if k == 0:
        return False
    if k == 1:
        return g.number_of_edges() == 0
    return (
        g.number_of_edges() == k - 1
        and nx.is_weakly_connected(g)
        and all(g.in_degree(v) <= 1 and g.out_degree(v) <= 1 for v in g)
    )


def _is_star(g: nx.DiGraph) -> bool:
    """Single root reaching every other node in exactly 1 hop, optionally plus
    ONE extra aggregator sink that every spoke feeds."""
    roots = [v for v in g if g.in_degree(v) == 0]
    if len(roots) != 1:
        return False
    root = roots[0]
    others = set(g) - {root}
    if not others:
        return False
    spokes = set(g.successors(root))
    if spokes == others:
        # Pure star: the root->spoke edges are the ONLY edges.
        return g.number_of_edges() == len(others)
    extra = others - spokes
    if len(extra) == 1 and spokes:
        (agg,) = extra
        return (
            g.out_degree(agg) == 0
            and all(set(g.successors(s)) == {agg} for s in spokes)
            and g.number_of_edges() == 2 * len(spokes)
        )
    return False


def _is_rooted_tree(g: nx.DiGraph) -> bool:
    """Tree from a single root: every non-root node has in-degree exactly 1.
    (Callers guarantee acyclicity and weak connectivity.)"""
    roots = [v for v in g if g.in_degree(v) == 0]
    return len(roots) == 1 and all(
        g.in_degree(v) == 1 for v in set(g) - {roots[0]}
    )


def _internal_density(g: nx.DiGraph, scc: set) -> float:
    """Directed edge density inside an SCC (self-loops excluded)."""
    k = len(scc)
    if k < 2:
        return 0.0
    internal = sum(
        1 for u, v in g.edges(scc) if u in scc and v in scc and u != v
    )
    return internal / (k * (k - 1))


def classify_topology(
    nodes: list[str], edges: list[tuple[str, str]]
) -> dict:
    """Classify the execution graph's shape per the frozen topology contract.

    Returns a dict with the structural attributes (node_count, edge_count,
    components, max_fan_out, depth, scc_count, bidirectional_pairs,
    articulation_points, chain) plus "primary": the archetype decided by the
    frozen first-match-wins order (disconnected > single_node > mesh >
    pipeline_with_feedback > cyclic_graph > pipeline > star > hierarchy > dag).
    """
    g = _build(nodes, edges)
    n = g.number_of_nodes()
    e = g.number_of_edges()

    components = nx.number_weakly_connected_components(g) if n else 0
    max_fan_out = max((d for _, d in g.out_degree()), default=0)

    nontrivial_sccs = [
        c for c in nx.strongly_connected_components(g) if len(c) >= 2
    ]
    cond = nx.condensation(g)
    depth = (
        nx.dag_longest_path_length(cond) + 1 if cond.number_of_nodes() else 0
    )
    bidirectional_pairs = sum(
        1 for u, v in g.edges if u < v and g.has_edge(v, u)
    )

    if n < 3:
        articulation_points: list[str] = []
    else:
        ug = nx.Graph()
        ug.add_nodes_from(g.nodes)
        ug.add_edges_from((u, v) for u, v in g.edges if u != v)
        articulation_points = sorted(nx.articulation_points(ug), key=str)

    # Primary archetype — frozen first-match-wins order.
    if components > 1:
        primary = "disconnected"
    elif n == 1:
        primary = "single_node"
    elif nontrivial_sccs and (
        bidirectional_pairs >= 2
        or any(
            len(c) >= 4 and _internal_density(g, c) >= 0.5
            for c in nontrivial_sccs
        )
    ):
        primary = "mesh"
    elif nontrivial_sccs and _is_simple_path(cond):
        primary = "pipeline_with_feedback"
    elif nontrivial_sccs:
        primary = "cyclic_graph"
    else:
        acyclic = nx.is_directed_acyclic_graph(g)
        if acyclic and _is_simple_path(g):
            primary = "pipeline"
        elif acyclic and _is_star(g):
            primary = "star"
        elif acyclic and _is_rooted_tree(g) and depth >= 3:
            primary = "hierarchy"
        else:
            primary = "dag"

    # CHAIN — decided by the same table, from the same predicate: an unbranched
    # line of 3+ steps through the condensation is exactly what "pipeline" and
    # "pipeline_with_feedback" already mean (``_is_simple_path`` on ``cond``),
    # and depth < 3 has no interior node for the shape to be silent about.
    # It is a PROPERTY of those archetypes, not a rival of them: the frozen
    # first-match-wins order above and every ``primary`` value it can return are
    # untouched, so the contract's mirrors keep classifying identically.
    chain = depth >= 3 and primary in ("pipeline", "pipeline_with_feedback")

    return {
        "node_count": n,
        "edge_count": e,
        "components": components,
        "max_fan_out": max_fan_out,
        "depth": depth,
        "scc_count": len(nontrivial_sccs),
        "bidirectional_pairs": bidirectional_pairs,
        "articulation_points": articulation_points,
        "primary": primary,
        # True when the shape cannot discriminate between nodes at all: blame
        # then rests on ordering, and attribution says so (see module docstring).
        "chain": chain,
    }
