"""Golden locks for the 8-archetype architecture battery (arch_*.json).

Each archetype fixture carries an injected fault; the golden locks the STABLE
blame surface (report_type, culprit_agents, deterministic signals, flags —
never confidences). The expected surfaces are ALSO hardcoded here so a wrong
re-record of a golden file cannot slip through silently: fixture -> replay,
replay -> golden, golden -> this table must all agree.

Richer behavior (confidence caps, loop-drill, shadowing wording, topology) is
locked engine-side in packages/blame_engine/tests/test_architectures.py.
"""

import json
from pathlib import Path

import pytest

from detective_ci import assert_matches_golden

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# archetype -> stable surface the golden must contain.
EXPECTED_SURFACES = {
    # One node, one failure: the cut point is the node itself.
    "single_agent": {
        "report_type": "cut_point",
        "culprit_agents": ["solo"],
        "deterministic_signals": [],
    },
    # Orchestrator -> 4 specialists -> aggregator; the failing specialist is
    # the origin, the aggregator only inherits (shadowed).
    "star": {
        "report_type": "cut_point",
        "culprit_agents": ["spec_code"],
        "deterministic_signals": [],
    },
    # manager -> leads -> workers -> merge; the deep worker is the origin.
    "hierarchy": {
        "report_type": "cut_point",
        "culprit_agents": ["worker_b1"],
        "deterministic_signals": [],
    },
    # Fully bidirectional 4-peer SCC: blame drills into the worst member of
    # the mesh (peer_alpha), not the exit peer that carried it downstream.
    "peer_mesh": {
        "report_type": "cut_point",
        "culprit_agents": ["peer_alpha"],
        "deterministic_signals": [],
    },
    # 5-stage chain, mid-stage break; downstream inherited/shadowed.
    "pipeline": {
        "report_type": "cut_point",
        "culprit_agents": ["draft"],
        "deterministic_signals": [],
    },
    # think->act->render->qa->eval with eval->act loopback; think silently
    # rewrites the file_type contract but every successor recovers and the
    # terminal is ok -> degraded_recovered with the deterministic breach.
    "graph_with_feedback": {
        "report_type": "degraded_recovered",
        "culprit_agents": ["think"],
        "deterministic_signals": ["contract_violation"],
        "flags": ["missing_required_content"],
    },
    # Fan-out bid market: the awarded winner delivered broken work; the
    # healthy losing bidders are never blamed (no multi_culprit).
    "market": {
        "report_type": "cut_point",
        "culprit_agents": ["bidder_b"],
        "deterministic_signals": [],
    },
    # 12-drone fixed dense swarm, two INDEPENDENT regional failures ->
    # multi_culprit with exactly those two origins; dense healthy neighbours
    # and shadowed inheritors stay unblamed.
    "swarm": {
        "report_type": "multi_culprit",
        "culprit_agents": ["drone_03", "drone_09"],
        "deterministic_signals": [],
    },
}

ARCHETYPES = sorted(EXPECTED_SURFACES)


@pytest.mark.parametrize("name", ARCHETYPES)
def test_archetype_replay_matches_golden(name: str) -> None:
    assert_matches_golden(
        EXAMPLES / f"arch_{name}.json", EXAMPLES / f"arch_{name}_golden.json"
    )


@pytest.mark.parametrize("name", ARCHETYPES)
def test_archetype_golden_contains_expected_surface(name: str) -> None:
    """Guards the golden FILES themselves: a bad re-record (e.g. after an
    engine regression) fails here even though replay==golden would pass."""
    golden = json.loads(
        (EXAMPLES / f"arch_{name}_golden.json").read_text(encoding="utf-8")
    )
    assert golden == EXPECTED_SURFACES[name]


@pytest.mark.parametrize("name", ARCHETYPES)
def test_archetype_golden_never_carries_confidence(name: str) -> None:
    """Confidences are judge-derived and non-reproducible: banned from goldens."""
    text = (EXAMPLES / f"arch_{name}_golden.json").read_text(encoding="utf-8")
    assert "confidence" not in text.lower()
