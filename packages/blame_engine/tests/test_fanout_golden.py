"""Fan-out / retry golden fixtures on the SCHEMA-2 layer (typed defects).

The archetype tests (test_architectures) lock culprits and candidacy prose for
non-linear topologies at the legacy level; the defect-evidence layer (typed
refs with polarity, channel derivation, propagation, validator) was designed
and tuned on ONE linear chain. These fixtures are the hand-authored ground
truth for the graph-first claims on the smallest non-trivial topologies:

- fan-out with an injection in ONE branch → the branch is the origin, the
  fan-in join is SHADOWED (inherited), never a second cause;
- fan-out with healthy branches and a bad JOIN → the join is the origin (the
  merge broke it), no branch is blamed;
- asymmetric branches → the propagation path follows the BAD branch through
  the join and never includes healthy siblings;
- two independently injected branches → multi_culprit with one typed content
  defect PER branch, each supported by its own branch-local evidence;
- a retry loop over the limit → a deterministic loop defect whose supporting
  ref is the loop_anomaly finding.

Expectations were written BEFORE running (keep-red discipline): a failure here
is a finding about the engine, never a reason to bend the assertion.
"""

import pytest

from blame_engine import Localized, TerminalVerdict, deserialize_defect, find_blame
from conftest import verdict_of


_BAD_TERMINAL = TerminalVerdict(
    bad=True, score=0.1, reasoning="the deliverable is empty", checkable=True
)

# start(structural root) -> orch -> w1,w2,w3 -> join -> (terminal is join's output)
_FANOUT_NODES = ["start", "orch", "w1", "w2", "w3", "join"]
_FANOUT_EDGES = [
    ("start", "orch"),
    ("orch", "w1"),
    ("orch", "w2"),
    ("orch", "w3"),
    ("w1", "join"),
    ("w2", "join"),
    ("w3", "join"),
]


def _fanout(mk, scores):
    return mk(
        nodes=_FANOUT_NODES,
        edges=_FANOUT_EDGES,
        scores={"start": None, **scores},
        terminal_verdict=_BAD_TERMINAL,
    )


def _typed(report):
    return report.evidence.findings, [
        deserialize_defect(d) for d in report.evidence.defects
    ]


def _supporting_kinds(findings, defect):
    return {
        findings[r.ref]["kind"]
        for r in defect.finding_refs
        if r.role == "supporting"
    }


def test_fanout_injected_branch_is_the_origin_join_is_shadowed(mk):
    """Injection in w2 only. The join drops too (it merged poisoned input) but
    is a DESCENDANT of the origin: shadowed, inherited, never a second cause.
    The typed content defect localizes at w2 with branch-local support."""
    report = find_blame(
        _fanout(mk, {"orch": 0.9, "w1": 0.9, "w2": 0.2, "w3": 0.9, "join": 0.3})
    )
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["w2"]
    assert verdict_of(report, "join") == "inherited"

    findings, defects = _typed(report)
    content = [d for d in defects if d.kind == "content"]
    assert len(content) == 1
    d = content[0]
    assert isinstance(d.origin, Localized) and d.origin.run_id == "w2"
    kinds = _supporting_kinds(findings, d)
    assert "content_drop" in kinds          # the measured drop localized it
    assert "terminal_content" in kinds      # bad terminal corroborates
    # No defect points at the join or at the healthy siblings.
    for other in defects:
        assert not (
            isinstance(other.origin, Localized)
            and other.origin.run_id in ("join", "w1", "w3")
        )


def test_fanout_healthy_branches_bad_join_blames_the_merge(mk):
    """All branches healthy, the join output is bad: the MERGE is where quality
    broke. cut_point at join; no branch is blamed."""
    report = find_blame(
        _fanout(mk, {"orch": 0.9, "w1": 0.9, "w2": 0.9, "w3": 0.9, "join": 0.2})
    )
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["join"]

    findings, defects = _typed(report)
    content = [d for d in defects if d.kind == "content"]
    assert len(content) == 1
    assert content[0].origin == Localized(run_id="join")
    assert "content_drop" in _supporting_kinds(findings, content[0])
    for healthy in ("w1", "w2", "w3", "orch"):
        assert verdict_of(report, healthy) == "healthy"


def test_fanout_propagation_path_follows_the_bad_branch_only(mk):
    """Asymmetric branches: the failure surfaced at the terminal ONLY through
    w2's branch. The propagation path is w2 -> join and must not include the
    healthy siblings (their outputs are not the vehicle of the fault)."""
    report = find_blame(
        _fanout(mk, {"orch": 0.9, "w1": 0.9, "w2": 0.2, "w3": 0.9, "join": 0.3})
    )
    assert report.propagation_path == ["w2", "join"]
    assert "w1" not in report.propagation_path
    assert "w3" not in report.propagation_path


def test_fanout_two_injected_branches_is_multi_culprit_with_per_branch_defects(mk):
    """w1 AND w3 fail independently (no shared non-root ancestry). Neither
    shadows the other -> multi_culprit, and the typed layer carries one content
    defect PER branch, each supported by its OWN branch-local drop — never one
    defect smeared over both origins."""
    report = find_blame(
        _fanout(mk, {"orch": 0.9, "w1": 0.2, "w2": 0.9, "w3": 0.2, "join": 0.3})
    )
    assert report.report_type == "multi_culprit"
    assert sorted(report.culprit_run_ids) == ["w1", "w3"]

    findings, defects = _typed(report)
    content = [d for d in defects if d.kind == "content"]
    origins = sorted(
        d.origin.run_id for d in content if isinstance(d.origin, Localized)
    )
    assert origins == ["w1", "w3"]
    for d in content:
        # Each defect's supporting drop finding is about ITS OWN branch.
        drop_refs = [
            r.ref
            for r in d.finding_refs
            if r.role == "supporting" and findings[r.ref]["kind"] == "content_drop"
        ]
        assert len(drop_refs) == 1
        assert findings[drop_refs[0]]["subject"] == f"run:{d.origin.run_id}"
    # The join is shadowed by both origins, never a third culprit.
    assert not any(
        isinstance(d.origin, Localized) and d.origin.run_id == "join"
        for d in defects
    )


def test_retry_loop_defect_is_deterministic_and_cites_the_anomaly(mk):
    """A retry cycle over max_loop_iterations: the loop defect is deterministic
    (a limit breach, not a judgement) and its supporting ref IS the
    loop_anomaly finding — members are the culprits, drilled as one loop."""
    nodes = [f"r{i}" for i in range(12)] + ["t"]
    edges = [(f"r{i}", f"r{(i + 1) % 12}") for i in range(12)] + [("r11", "t")]
    inp = mk(
        nodes=nodes,
        edges=edges,
        scores={n: 0.9 for n in nodes},
        agent_names={**{f"r{i}": "worker" for i in range(12)}, "t": "t"},
    )
    report = find_blame(inp)
    assert report.report_type == "loop_detected"
    assert report.culprit_run_ids == [f"r{i}" for i in range(12)]

    findings, defects = _typed(report)
    loop = [d for d in defects if d.kind == "loop"]
    assert len(loop) == 1
    d = loop[0]
    assert d.channel == "deterministic"
    assert isinstance(d.origin, Localized) and d.origin.run_id == "r0"
    assert _supporting_kinds(findings, d) == {"loop_anomaly"}
    anomaly_ref = next(r.ref for r in d.finding_refs if r.role == "supporting")
    assert findings[anomaly_ref]["data"]["iterations"] == 12
    assert findings[anomaly_ref]["data"]["members"] == [f"r{i}" for i in range(12)]
