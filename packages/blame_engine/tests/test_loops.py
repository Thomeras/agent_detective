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
