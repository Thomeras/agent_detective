"""S7: the flagship demo shape — diamond graph, scraper breaks, downstream
nodes inherit the degradation with small drops."""

import pytest

from blame_engine import find_blame


def test_flagship_diamond_cut_point(mk) -> None:
    inp = mk(
        nodes=["orch", "scraper", "translator", "compliance", "publisher"],
        edges=[
            ("orch", "scraper"),
            ("orch", "translator"),
            ("scraper", "compliance"),
            ("translator", "compliance"),
            ("compliance", "publisher"),
        ],
        scores={
            "orch": 1.0,
            "scraper": 0.2,
            "translator": 0.9,
            "compliance": 0.45,  # inherited via scraper; shadowed candidate
            "publisher": 0.4,    # inherited, drop 0.05 < min_drop
        },
        costs={"orch": 5.0, "scraper": 0.5, "translator": 3.0,
               "compliance": 1.0, "publisher": 2.0},
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["scraper"]
    assert report.evidence.drops == {"scraper": pytest.approx(0.8), "compliance": pytest.approx(0.45)}
    assert report.propagation_path == ["scraper", "compliance", "publisher"]
    # Culprit + descendants only: scraper, compliance, publisher.
    assert report.downstream_cost_usd == pytest.approx(3.5)
    # gap=1.0, severity=0.6, pred=1.0 -> 0.5 + 0.18 + 0.2, no penalties
    assert report.confidence == pytest.approx(0.88)
    assert report.unscored_run_ids == []
