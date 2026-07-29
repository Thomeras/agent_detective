"""Loop anomaly scenarios: max_iterations (S2), statistical baseline (S3),
anomalous loop coexisting with a clear cut point elsewhere (S6)."""

import pytest

from blame_engine import BlameConfig, LoopBaseline, detect_loop_anomalies, find_blame


def _cycle_edges(prefix: str, n: int) -> list[tuple[str, str]]:
    return [(f"{prefix}{i}", f"{prefix}{(i + 1) % n}") for i in range(n)]


def test_loop_over_max_iterations_is_loop_detected(mk) -> None:
    """S2: 12 iterations > max_loop_iterations=10 -> loop_detected, members culprit."""
    nodes = [f"l{i}" for i in range(12)] + ["t"]
    edges = _cycle_edges("l", 12) + [("l11", "t")]
    inp = mk(nodes=nodes, edges=edges, scores={n: 0.9 for n in nodes})
    report = find_blame(inp)

    assert report.report_type == "loop_detected"
    assert report.culprit_run_ids == [f"l{i}" for i in range(12)]
    assert report.confidence == pytest.approx(1.0)
    assert len(report.evidence.loop_anomalies) == 1
    anomaly = report.evidence.loop_anomalies[0]
    assert anomaly.limit_kind == "max_iterations"
    assert anomaly.iterations == 12
    assert anomaly.baseline is None
    assert report.propagation_path == nodes  # members expanded, then terminal


def test_statistical_anomaly_fires(mk) -> None:
    """S3: baseline mean=3 std=1 n=5, observed 9 > 3 + 3*1 -> statistical anomaly."""
    nodes = [f"l{i}" for i in range(9)]
    inp = mk(
        nodes=nodes,
        edges=_cycle_edges("l", 9),
        scores={n: 0.9 for n in nodes},
        agent_names={n: "looper" for n in nodes},
        loop_baselines={"looper": LoopBaseline(mean_iterations=3.0, std_iterations=1.0, sample_count=5)},
    )
    anomalies = detect_loop_anomalies(inp)
    assert len(anomalies) == 1
    assert anomalies[0].limit_kind == "statistical"
    assert anomalies[0].iterations == 9
    assert anomalies[0].baseline is not None
    assert anomalies[0].agent_names == ["looper"] * 9

    assert find_blame(inp).report_type == "loop_detected"


def test_statistical_anomaly_below_zscore_does_not_fire(mk) -> None:
    """S3: observed 4 <= 3 + 3*1 -> no anomaly."""
    nodes = [f"l{i}" for i in range(4)]
    inp = mk(
        nodes=nodes,
        edges=_cycle_edges("l", 4),
        scores={n: 0.9 for n in nodes},
        agent_names={n: "looper" for n in nodes},
        loop_baselines={"looper": LoopBaseline(mean_iterations=3.0, std_iterations=1.0, sample_count=5)},
    )
    assert detect_loop_anomalies(inp) == []
    assert find_blame(inp).report_type != "loop_detected"


def test_baseline_with_insufficient_history_ignored(mk) -> None:
    """S3: sample_count < loop_min_history -> baseline ignored entirely."""
    nodes = [f"l{i}" for i in range(9)]
    inp = mk(
        nodes=nodes,
        edges=_cycle_edges("l", 9),
        scores={n: 0.9 for n in nodes},
        agent_names={n: "looper" for n in nodes},
        loop_baselines={"looper": LoopBaseline(mean_iterations=3.0, std_iterations=1.0, sample_count=4)},
    )
    assert detect_loop_anomalies(inp) == []
    assert find_blame(inp).report_type != "loop_detected"


def test_anomalous_loop_with_cut_point_elsewhere(mk) -> None:
    """S6: anomalous loop + clear cut point elsewhere -> primary report is
    cut_point; the anomaly only lands in evidence."""
    nodes = [f"l{i}" for i in range(4)] + ["x", "y"]
    edges = _cycle_edges("l", 4) + [("l3", "x"), ("x", "y")]
    inp = mk(
        nodes=nodes,
        edges=edges,
        scores={"l0": 0.9, "l1": 0.9, "l2": 0.9, "l3": 0.9, "x": 1.0, "y": 0.2},
        config=BlameConfig(max_loop_iterations=3),
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["y"]
    assert len(report.evidence.loop_anomalies) == 1
    assert report.evidence.loop_anomalies[0].limit_kind == "max_iterations"


# --- rounds vs cycle size -------------------------------------------------
#
# The check bounds ITERATIONS ("burned iterations past the limit"), and cycle
# size is not that number. A bounded nested loop (2 outer x 3 inner, every
# bound a literal `range()`) condenses into a 21-node SCC and was reported as
# 21 runaway iterations at 100% confidence, with all 21 members named origin.
# Found by running topologies/13_nested_loops from agent_topo_db through the
# CLI: nothing ran away, and the report named the entire graph.


def _nested_loop_input(mk, *, outer=2, inner=3, config=None, baselines=None):
    """One SCC the way an instrumented nested loop condenses: the busiest agent
    (`builder`) runs once per outer round plus once per inner round."""
    nodes, edges, attempts, agent_names = ["ctrl"], [], {}, {"ctrl": "ctrl"}
    previous = "ctrl"
    count = 0
    for _ in range(outer * (inner + 1)):
        count += 1
        node = f"b{count}"
        nodes.append(node)
        agent_names[node] = f"builder#{count}"   # attempts need distinct names
        attempts[node] = ("builder", count)
        edges.append((previous, node))
        previous = node
    edges.append((previous, "ctrl"))             # the back-edge closes the loop
    return mk(
        nodes=nodes, edges=edges, attempts=attempts, agent_names=agent_names,
        scores={n: 0.9 for n in nodes}, config=config,
        loop_baselines=baselines,
    )


def test_bounded_nested_loop_is_not_a_runaway(mk) -> None:
    """8 rounds inside a 9-member cycle, limit 10 -> no anomaly. The real trace
    that surfaced this had 21 members and fired at 100%."""
    assert detect_loop_anomalies(_nested_loop_input(mk)) == []
    assert find_blame(_nested_loop_input(mk)).report_type != "loop_detected"


def test_rounds_are_counted_not_members(mk) -> None:
    """12 rounds of one agent > 10 -> still detected, and reported as 12 even
    though the cycle has 13 members."""
    inp = _nested_loop_input(mk, outer=3, inner=3)
    anomalies = detect_loop_anomalies(inp)
    assert len(anomalies) == 1
    assert anomalies[0].iterations == 12
    assert len(anomalies[0].member_run_ids) == 13


def test_only_the_repeating_runs_are_blamed(mk) -> None:
    """The controller is caught in the cycle; it is not what ran away."""
    report = find_blame(_nested_loop_input(mk, outer=3, inner=3))
    assert report.report_type == "loop_detected"
    assert "ctrl" not in report.culprit_run_ids
    assert report.culprit_run_ids == [f"b{i}" for i in range(1, 13)]


def test_baselines_key_on_the_agent_not_the_attempt(mk) -> None:
    """An attempt's agent_name is per-attempt (`builder#7`), so a baseline
    recorded for `builder` was looked up under a name that occurs exactly once
    and never matched."""
    inp = _nested_loop_input(
        mk, outer=2, inner=2,
        config=BlameConfig(max_loop_iterations=100),
        baselines={
            "builder": LoopBaseline(
                mean_iterations=2.0, std_iterations=0.5, sample_count=5
            )
        },
    )
    anomalies = detect_loop_anomalies(inp)
    assert len(anomalies) == 1
    assert anomalies[0].limit_kind == "statistical"


def test_uninstrumented_cycles_keep_the_old_count(mk) -> None:
    """No loop identity in the trace -> member count is all there is, and the
    pre-existing behaviour is unchanged."""
    nodes = [f"l{i}" for i in range(12)]
    inp = mk(nodes=nodes, edges=_cycle_edges("l", 12), scores={n: 0.9 for n in nodes})
    anomalies = detect_loop_anomalies(inp)
    assert len(anomalies) == 1
    assert anomalies[0].iterations == 12
    assert anomalies[0].repeating_run_ids == []
