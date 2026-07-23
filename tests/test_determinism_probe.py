"""Unit tests for the pure summary math in scripts/determinism_probe.py.

Unlike tests/e2e, these need NO live stack (the probe's HTTP layer is not
exercised here) and must always run: ``uv run pytest tests`` from the repo
root covers them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from determinism_probe import NO_REPORT, extract_round, node_labels, summarize_rounds  # noqa: E402


def make_round(
    report_type: str = "cut_point",
    culprits: list[str] | None = None,
    confidence: float | None = 0.72,
    attribution: float | None = 0.6,
    scores: dict[str, float | None] | None = None,
) -> dict:
    return {
        "report_type": report_type,
        "culprits": culprits if culprits is not None else ["scraper-agent"],
        "confidence": confidence,
        "attribution_confidence": attribution,
        "scores": scores if scores is not None else {"scraper-agent": 0.2, "publisher-agent": 0.9},
    }


def test_identical_rounds_are_stable():
    summary = summarize_rounds([make_round() for _ in range(10)])
    assert summary["rounds"] == 10
    assert summary["stable"] is True
    assert summary["inconclusive"] is False
    assert summary["verdict_distribution"] == {"cut_point|culprits=scraper-agent": 10}
    assert summary["culprit_stability"] == {
        "modal_culprits": ["scraper-agent"],
        "modal_fraction": 1.0,
        "distinct_sets": 1,
    }
    assert summary["confidence"]["stddev"] == 0.0
    assert summary["confidence"]["mean"] == pytest.approx(0.72)


def test_verdict_flip_is_unstable_with_split_distribution():
    rounds = [make_round() for _ in range(7)] + [
        make_round(report_type="degraded_recovered", culprits=["compliance-agent"])
        for _ in range(3)
    ]
    summary = summarize_rounds(rounds)
    assert summary["stable"] is False
    assert summary["verdict_distribution"] == {
        "cut_point|culprits=scraper-agent": 7,
        "degraded_recovered|culprits=compliance-agent": 3,
    }
    assert summary["culprit_stability"]["modal_culprits"] == ["scraper-agent"]
    assert summary["culprit_stability"]["modal_fraction"] == pytest.approx(0.7)
    assert summary["culprit_stability"]["distinct_sets"] == 2


def test_same_type_different_culprits_is_unstable():
    rounds = [make_round(culprits=["a"]), make_round(culprits=["b"])]
    assert summarize_rounds(rounds)["stable"] is False


def test_node_score_stats_and_flip_risk():
    rounds = [
        make_round(scores={"edge-node": 0.45, "solid-node": 0.95}),
        make_round(scores={"edge-node": 0.55, "solid-node": 0.95}),
        make_round(scores={"edge-node": 0.50, "solid-node": 0.95}),
    ]
    summary = summarize_rounds(rounds)
    edge = summary["node_scores"]["edge-node"]
    assert edge["mean"] == pytest.approx(0.5)
    assert edge["min"] == 0.45
    assert edge["max"] == 0.55
    assert edge["stddev"] == pytest.approx(0.0408, abs=1e-3)
    assert edge["flip_risk"] is True  # mean within 0.10 of the 0.50 threshold
    assert edge["crossed_threshold"] is True  # scores landed on both sides

    solid = summary["node_scores"]["solid-node"]
    assert solid["stddev"] == 0.0
    assert solid["flip_risk"] is False
    assert solid["crossed_threshold"] is False

    assert summary["flip_risks"] == ["edge-node"]


def test_flip_risk_band_edges():
    # 0.60 is exactly threshold+band -> still flagged; 0.61 is not.
    at_band = summarize_rounds([make_round(scores={"n": 0.60})])
    beyond = summarize_rounds([make_round(scores={"n": 0.61})])
    assert at_band["node_scores"]["n"]["flip_risk"] is True
    assert beyond["node_scores"]["n"]["flip_risk"] is False


def test_none_scores_and_confidences_are_skipped():
    rounds = [
        make_round(scores={"a": None, "b": 0.8}, confidence=None, attribution=None),
        make_round(scores={"a": 0.7, "b": 0.8}, confidence=0.5, attribution=None),
    ]
    summary = summarize_rounds(rounds)
    assert summary["node_scores"]["a"]["n"] == 1
    assert summary["node_scores"]["b"]["n"] == 2
    assert summary["confidence"]["n"] == 1
    assert summary["attribution_confidence"] is None


def test_all_no_report_rounds_are_inconclusive_not_stable():
    rounds = [
        {"report_type": NO_REPORT, "culprits": [], "confidence": None,
         "attribution_confidence": None, "scores": {}}
        for _ in range(3)
    ]
    summary = summarize_rounds(rounds)
    assert summary["inconclusive"] is True
    assert summary["stable"] is False
    assert summary["no_report_rounds"] == 3


def test_mixed_no_report_counts_as_instability():
    rounds = [make_round(), {"report_type": NO_REPORT, "culprits": [], "confidence": None,
                             "attribution_confidence": None, "scores": {}}]
    summary = summarize_rounds(rounds)
    assert summary["inconclusive"] is False
    assert summary["stable"] is False
    assert summary["no_report_rounds"] == 1


def test_empty_rounds_are_inconclusive():
    summary = summarize_rounds([])
    assert summary["rounds"] == 0
    assert summary["inconclusive"] is True
    assert summary["stable"] is False


# --- extract_round / node_labels glue (still no stack needed) ---


def test_extract_round_maps_run_ids_to_agent_labels():
    labels = {"run-1": "scraper-agent", "run-2": "publisher-agent"}
    report = {
        "id": 7,
        "version": 3,
        "report_type": "cut_point",
        "culprit_run_ids": ["run-1"],
        "confidence": 0.66,
        "evidence": {
            "attribution_confidence": 0.55,
            "score_map": {"run-1": 0.2, "run-2": None},
        },
    }
    observed = extract_round(report, labels)
    assert observed["report_type"] == "cut_point"
    assert observed["culprits"] == ["scraper-agent"]
    assert observed["confidence"] == 0.66
    assert observed["attribution_confidence"] == 0.55
    assert observed["scores"] == {"scraper-agent": 0.2, "publisher-agent": None}
    assert observed["report_version"] == 3


def test_extract_round_none_report_is_no_report_sentinel():
    observed = extract_round(None, {})
    assert observed["report_type"] == NO_REPORT
    assert observed["culprits"] == []
    assert observed["scores"] == {}


def test_node_labels_disambiguates_duplicate_agent_names():
    graph = {
        "nodes": [
            {"data": {"id": "aaaaaaaa-1111", "agent_name": "worker"}},
            {"data": {"id": "bbbbbbbb-2222", "agent_name": "worker"}},
            {"data": {"id": "cccccccc-3333", "agent_name": "publisher"}},
        ]
    }
    labels = node_labels(graph)
    assert labels["cccccccc-3333"] == "publisher"
    assert labels["aaaaaaaa-1111"] == "worker:aaaaaaaa"
    assert labels["bbbbbbbb-2222"] == "worker:bbbbbbbb"
