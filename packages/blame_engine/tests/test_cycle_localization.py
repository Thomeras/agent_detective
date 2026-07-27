"""Localization INSIDE cycles, and the rows that used to lose a fault.

Every case here was a demonstrated wrong verdict, not a hypothesis:

- the orchestrator + sub-agents-as-tools shape (``SPAWN`` out,
  ``TOOL_DELEGATION`` back) condenses to ONE super-node scored by its exit —
  the orchestrator, which ends last — so a sub-agent at 0.10 was invisible and
  the verdict landed on a downstream node or on "the orchestration layer";
- ``composition_failure`` asserted "no node individually failed (all scores
  above threshold)" beside a score map showing 0.10, because its guard read
  super-nodes while the report renders raw ones;
- an anomalous loop replaced every other defect, so a second, independent
  break in another branch vanished entirely;
- the multi-culprit row never drilled, so the same fault named the cycle's
  EXIT (which can sit above the threshold) whenever a second fault existed.
"""

import pytest

from blame_engine import NodeScore, TerminalVerdict, find_blame
from conftest import candidacy_of, note_of, verdict_of
from blame_engine.types import BlameConfig

BAD = TerminalVerdict(bad=True, score=0.2, reasoning="deliverable is wrong")
OK = TerminalVerdict(bad=False, score=0.95, reasoning="deliverable meets the goal")


# --- the orchestrator/delegation cycle ------------------------------------


def _supervisor(mk, **kw):
    """orchestrator ⇄ each worker (SPAWN out + TOOL_DELEGATION back), then a
    final node. The orchestrator wraps the whole run, so it ends LAST and is the
    cycle's exit node."""
    return mk(
        nodes=["sup", "w1", "w2", "w3", "final"],
        edges=[
            ("sup", "w1"), ("sup", "w2"), ("sup", "w3"),
            ("w1", "sup"), ("w2", "sup"), ("w3", "sup"),
            ("sup", "final"),
        ],
        scores={"sup": 0.9, "w1": 0.9, "w2": 0.15, "w3": 0.9, "final": 0.4},
        end_times={"sup": 10.0, "w1": 1.0, "w2": 2.0, "w3": 3.0, "final": 11.0},
        **kw,
    )


def test_broken_sub_agent_inside_a_healthy_exit_cycle_is_the_culprit(mk) -> None:
    report = find_blame(_supervisor(mk, terminal_verdict=BAD))

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["w2"]
    # Measured against w2's real predecessor inside the cycle (sup 0.90).
    assert report.evidence.drops["w2"] == pytest.approx(0.75)


def test_downstream_node_is_not_blamed_for_the_cycles_output(mk) -> None:
    """'final' dropped 0.50 from the cycle's exit score and used to be named the
    cut point — it inherited the broken sub-agent's work through the exit."""
    report = find_blame(_supervisor(mk, terminal_verdict=BAD))

    assert "final" not in report.culprit_run_ids
    assert verdict_of(report, "final") == "inherited"


def test_cycle_headline_does_not_call_a_delegation_pair_a_retry_loop(mk) -> None:
    report = find_blame(_supervisor(mk, terminal_verdict=BAD))
    # The cut_point's loop variants describe a CYCLE — the engine sees no edge
    # types, so no template in the table may call one a retry loop.
    assert note_of(report, "cut_point")["variant"] in ("loop", "loop_unmeasured")


def test_unscored_orchestrator_no_longer_swallows_the_whole_analysis(mk) -> None:
    """The orchestrator has no payload of its own, so the cycle's score is
    UNKNOWN. That used to yield `unclassified`, zero culprits and $0 cost with a
    visibly broken worker in the score map."""
    inp = mk(
        nodes=["orch", "w1", "w2", "final"],
        edges=[("orch", "w1"), ("w1", "orch"), ("orch", "w2"), ("w2", "orch"),
               ("orch", "final")],
        scores={"orch": None, "w1": 0.9, "w2": 0.1, "final": 0.85},
        end_times={"orch": 9.0, "w1": 1.0, "w2": 2.0, "final": 10.0},
        terminal_verdict=BAD,
    )
    report = find_blame(inp)

    assert report.culprit_run_ids == ["w2"]
    # No scored predecessor inside the cycle: honest, low attribution — and the
    # note says why rather than printing a drop it cannot measure.
    assert report.confidence < 0.3
    # The unmeasured variant is the one that says "no scored predecessor inside
    # the cycle" instead of printing a drop it cannot measure.
    assert note_of(report, "cut_point")["variant"] == "loop_unmeasured"


def test_a_repaired_iteration_is_a_near_miss_not_a_cut_point(mk) -> None:
    """Reaching inside cycles must not report every successful retry as a break:
    the bad iteration's successor came out healthy and the terminal is ok."""
    inp = mk(
        nodes=["a", "b", "c", "t"],
        edges=[("a", "b"), ("b", "c"), ("c", "a"), ("c", "t")],
        scores={"a": 0.9, "b": 0.1, "c": 0.9, "t": 0.9},
        terminal_verdict=OK,
    )
    report = find_blame(inp)

    assert report.report_type == "degraded_recovered"
    assert report.culprit_run_ids == ["b"]


def test_cycle_member_exactly_at_the_threshold_keeps_its_evidence(mk) -> None:
    """Regression: ``find_blame`` RAISED on this graph (found by the fuzzer).

    n2 sits exactly AT the threshold (0.50 — so it is not "degraded") while
    having dropped 0.50 from a healthy predecessor inside the cycle. Two rules
    then collide: the content-drop finding is skipped at build time for every
    cycle member (its drop is measured at the loop exit, so blame emits the
    drilled member's real one instead), and a score of 0.50 is not BELOW
    threshold, so it cannot stand in as supporting evidence either. The content
    defect reached the §2.4 validator citing nothing that asserts it, the
    validator correctly refused to build it — and took the whole analysis down
    with a ValueError.

    In tier2 that is not a bad report. It is NO report: the graph is ingested,
    the worker throws, and the run silently never gets analysed at all.
    """
    inp = mk(
        nodes=["n0", "n1", "n2", "n3"],
        edges=[("n0", "n2"), ("n2", "n0")],
        scores={"n0": 1.0, "n1": None, "n2": 0.5, "n3": None},
        terminal_verdict=OK,
    )
    report = find_blame(inp)  # must not raise

    assert report.culprit_run_ids == ["n2"]
    content = next(d for d in report.evidence.defects if d["kind"] == "content")
    supporting = [r for r in content["finding_refs"] if r["role"] == "supporting"]
    assert supporting, "content defect built with no supporting finding"
    # The evidence is the measured in-cycle drop, not the (healthy) raw score.
    kinds = {report.evidence.findings[r["ref"]]["kind"] for r in supporting}
    assert "content_drop" in kinds
    # And the headline must READ that evidence. degraded_recovered leads with the
    # observation, which used to be severity-only: at a score of exactly 0.50 it
    # reported "0 %" beside a verdict naming this node — a report that looks
    # broken while holding a measured 0.50 drop.
    assert report.confidence > 0.0
    assert report.confidence == pytest.approx(report.evidence.observation_confidence)


# --- the verdict may not contradict its own score map ---------------------


def test_composition_failure_cannot_fire_over_a_sub_threshold_cycle_member(mk) -> None:
    """A peer cycle whose members are all degraded EXCEPT the exit: nothing
    qualifies as an origin (no member had a healthy predecessor to break from),
    but 'no node individually failed' would be a lie about the score map."""
    inp = mk(
        nodes=["p1", "p2", "p3", "out"],
        edges=[("p1", "p2"), ("p2", "p1"), ("p2", "p3"), ("p3", "p2"),
               ("p3", "out")],
        scores={"p1": 0.3, "p2": 0.35, "p3": 0.85, "out": 0.85},
        end_times={"p1": 1.0, "p2": 2.0, "p3": 3.0, "out": 4.0},
        terminal_verdict=BAD,
    )
    report = find_blame(inp)

    assert report.report_type != "composition_failure"
    assert note_of(report, "composition_failure") is None


def test_below_threshold_node_is_not_called_shadowed_when_nothing_localized(mk) -> None:
    inp = mk(
        nodes=["p1", "p2", "p3", "out"],
        edges=[("p1", "p2"), ("p2", "p1"), ("p2", "p3"), ("p3", "p2"),
               ("p3", "out")],
        scores={"p1": 0.3, "p2": 0.35, "p3": 0.85, "out": 0.85},
        end_times={"p1": 1.0, "p2": 2.0, "p3": 3.0, "out": 4.0},
        terminal_verdict=BAD,
    )
    report = find_blame(inp)

    assert not report.culprit_run_ids
    assert verdict_of(report, "p1") == "below_not_origin"


# --- a loop no longer absorbs the rest of the graph -----------------------


def test_anomalous_loop_does_not_swallow_an_independent_origin(mk) -> None:
    inp = mk(
        nodes=["src", "a1", "a2", "b1", "join"],
        edges=[("src", "a1"), ("a1", "a2"), ("a2", "a1"), ("a2", "join"),
               ("src", "b1"), ("b1", "join")],
        scores={"src": 0.95, "a1": 0.2, "a2": 0.3, "b1": 0.2, "join": 0.3},
        config=BlameConfig(max_loop_iterations=1),
    )
    report = find_blame(inp)

    assert report.report_type == "multi_culprit"
    assert "b1" in report.culprit_run_ids          # the independent break
    assert "a1" in report.culprit_run_ids          # the loop members
    kinds = {d["kind"] for d in report.evidence.defects}
    assert kinds == {"loop", "content"}            # the loop defect SURVIVES


def test_lone_anomalous_loop_still_reports_loop_detected(mk) -> None:
    inp = mk(
        nodes=["src", "a1", "a2", "out"],
        edges=[("src", "a1"), ("a1", "a2"), ("a2", "a1"), ("a2", "out")],
        scores={"src": 0.95, "a1": 0.2, "a2": 0.3, "out": 0.3},
        config=BlameConfig(max_loop_iterations=1),
    )
    assert find_blame(inp).report_type == "loop_detected"


# --- one fault localizes the same way regardless of how many faults exist --


def test_multi_culprit_drills_into_the_cycle_like_the_single_row_does(mk) -> None:
    """l2 (0.60) is the cycle's exit and sits ABOVE the threshold; l1 (0.10) is
    where quality broke. Alone, blame named l1; alongside a second origin it
    named l2 — a healthy-scoring node reported as a culprit."""
    inp = mk(
        nodes=["src", "l1", "l2", "b1", "join"],
        edges=[("src", "l1"), ("l1", "l2"), ("l2", "l1"), ("l2", "join"),
               ("src", "b1"), ("b1", "join")],
        scores={"src": 0.95, "l1": 0.1, "l2": 0.6, "b1": 0.2, "join": 0.5},
    )
    report = find_blame(inp)

    assert report.report_type == "multi_culprit"
    assert set(report.culprit_run_ids) == {"l1", "b1"}
    assert "l2" not in report.culprit_run_ids


# --- a join is not the culprit for a branch it only merged -----------------


def test_join_is_not_blamed_when_a_branch_arrived_already_broken(mk) -> None:
    """The join's BEST predecessor is healthy (0.95) but its worst is 0.20:
    quality was not "fine going in". The bad branch is a real source of flawed
    input, so the fault is external, not a break at the merge."""
    inp = mk(
        nodes=["good", "bad", "join"],
        edges=[("good", "join"), ("bad", "join")],
        scores={
            "good": 0.95,
            "bad": NodeScore(run_id="bad", score=0.2, components={},
                             input_flawed=True, unscored_reason=None,
                             judge_note=None),
            "join": 0.45,
        },
        terminal_verdict=BAD,
    )
    report = find_blame(inp)

    assert report.report_type == "root_cause_external"
    assert "join" not in report.culprit_run_ids


def test_join_with_an_unscored_branch_declares_the_unmeasured_input(mk) -> None:
    """One branch never scored: the join can still be the origin (a single judge
    error must not blind the analysis) but the trace may not imply a clean
    handoff it never observed."""
    inp = mk(
        nodes=["src", "good", "bad", "join"],
        edges=[("src", "good"), ("src", "bad"), ("good", "join"), ("bad", "join")],
        scores={"src": 0.95, "good": 0.95, "bad": None, "join": 0.45},
        terminal_verdict=BAD,
    )
    report = find_blame(inp)

    assert report.culprit_run_ids == ["join"]
    assert candidacy_of(report, "join")["unmeasured_inputs"] == ["bad"]


def test_input_flawed_claim_refuted_by_a_healthy_predecessor_does_not_excuse(mk) -> None:
    """A PRODUCER whose judge claimed "my input was already flawed" while its
    only scored predecessor came out at 0.95: the claim is contradicted by the
    score map, so it may not clear the node and push blame downstream."""
    inp = mk(
        nodes=["src", "mid", "out"],
        edges=[("src", "mid"), ("mid", "out")],
        scores={
            "src": 0.95,
            "mid": NodeScore(run_id="mid", score=0.2, components={},
                             input_flawed=True, unscored_reason=None,
                             judge_note=None),
            "out": 0.3,
        },
        terminal_verdict=BAD,
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["mid"]


# --- instrumentation artifacts are not unobserved agents -------------------


def test_edge_to_an_unknown_run_does_not_invent_a_node(mk) -> None:
    inp = mk(
        nodes=["a", "b"],
        edges=[("a", "b"), ("b", "ghost")],
        scores={"a": 0.9, "b": 0.9},
    )
    report = find_blame(inp)

    assert report.unscored_run_ids == []
    assert "ghost" not in report.evidence.score_map
