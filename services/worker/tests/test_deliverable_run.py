"""deliverable_run: pick the run whose output is the real deliverable.

Regression for the docx phantom — a QA retry loopback (eval -> act) leaves every
node with an outgoing edge, so the naive sink fallback grabbed the orchestrator
ROOT (no output) and the terminal judge hallucinated 'final output is empty'.
"""

from conftest import make_bundle, make_run, uid

from worker.graph_ops import deliverable_run


def _foundry_bundle():
    """start -> think -> act -> render -> qa -> eval, with an eval->act retry
    loopback so NO node is a plain sink."""
    runs = [
        make_run(1, "start", input_inline="req", output_inline=None, end_time=100.0),
        make_run(2, "think", end_time=10.0),
        make_run(3, "act", end_time=20.0),
        make_run(4, "render", output_inline="artifact_text: full document body", end_time=30.0),
        make_run(5, "qa", output_inline='{"pass": true}', end_time=40.0),
        make_run(6, "eval", output_inline='{"pass": false}', end_time=50.0),
    ]
    edges = [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 3)]  # 6->3 = retry loopback
    return make_bundle(runs, edges)


def test_loopback_does_not_select_the_root_wrapper():
    """The root 'start' (no output) must never be the deliverable, even when the
    loopback makes it the latest-ended run with no true sink."""
    d = deliverable_run(_foundry_bundle())
    assert d is not None
    assert d.agent_name != "start"


def test_verifier_sink_walks_back_to_the_producer():
    """eval/qa emit verdicts, not the artifact — deliverable is render's output."""
    d = deliverable_run(_foundry_bundle())
    assert d.run_id == uid(4)  # render
    assert "full document body" in (d.output_inline or "")


def test_plain_producer_sink_is_the_deliverable():
    """No verifier, a normal sink: return it directly."""
    runs = [
        make_run(1, "planner", end_time=1.0),
        make_run(2, "writer", output_inline="the deliverable", end_time=2.0),
    ]
    d = deliverable_run(make_bundle(runs, [(1, 2)]))
    assert d.run_id == uid(2)
