"""Degraded-but-recovered verdict + split confidence + manifestation gate +
contract-vs-terminal cross-check.

These encode the fixes for the live generative_simon near-miss: a node (think)
that silently rewrote a carried contract (file_type docx->md) and produced only
an outline, which the downstream nodes compensated for while the terminal
deliverable stayed ok. The old engine cried a cut_point "where quality broke" at
0.21 confidence with a phantom "failure surfaced in render"; the honest verdict
is a recovered near-miss with high observation confidence and no manifestation.
"""

import pytest

from blame_engine import NodeScore, TerminalVerdict, find_blame
from conftest import candidacy_of, note_of


def _score(run_id, value, *, contract=(), flags=(), note="judged"):
    """A scored node with optional deterministic contract violations / flags."""
    return NodeScore(
        run_id=run_id,
        score=value,
        components={},
        input_flawed=False,
        unscored_reason=None,
        judge_note=note,
        flags=tuple(flags),
        contract_violations=tuple(contract),
    )


def _pipeline(think_score, *, contract=(), flags=(), terminal):
    """start(structural root) -> think -> act -> render -> qa -> eval, with think
    the only degraded node and everything downstream healthy."""
    return dict(
        nodes=["start", "think", "act", "render", "qa", "eval"],
        edges=[("start", "think"), ("think", "act"), ("act", "render"),
               ("render", "qa"), ("qa", "eval")],
        scores={
            "start": None,  # payload_missing -> structural root
            "think": _score("think", think_score, contract=contract, flags=flags,
                            note="only an outline, no content"),
            "act": _score("act", 0.93),
            "render": _score("render", 0.93),
            "qa": _score("qa", 1.0),
            "eval": _score("eval", 1.0),
        },
        terminal_verdict=terminal,
    )


_TERMINAL_OK = TerminalVerdict(bad=False, score=1.0, reasoning="comprehensive overview, aligns with the request", checkable=True)
_TERMINAL_BAD = TerminalVerdict(bad=True, score=0.1, reasoning="empty / missing content", checkable=True)


def test_recovered_contract_violation_is_degraded_recovered_not_cut_point(mk):
    """The live scenario: think degraded + silent contract rewrite, but every
    successor and the terminal recovered -> degraded_recovered, NOT a cut_point
    alarm."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")],
                         flags=["missing_required_content"], terminal=_TERMINAL_OK))
    report = find_blame(inp)

    assert report.report_type == "degraded_recovered"
    assert report.culprit_run_ids == ["think"]


def test_degraded_recovered_confidence_is_split_and_honest(mk):
    """Headline = observation (near-certain: a deterministic contract breach).
    Attribution is high too (clean handoff from a structural root, so the drop is
    real), and both are surfaced — the certain finding is never buried at 0.21."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")],
                         flags=["missing_required_content"], terminal=_TERMINAL_OK))
    ev = find_blame(inp).evidence

    # Deterministic defect -> observation near-certain.
    assert ev.observation_confidence == pytest.approx(0.95)
    # The verdict is carried by the contract violation, whose origination is
    # OBSERVED (the input/output diff saw the parameter arrive intact and leave
    # rewritten) — the observability-boundary cap does not apply to it. Headline
    # attribution = the verdict-carrying defect's attribution, never a blended
    # ceiling that matches no defect.
    assert ev.attribution_confidence == pytest.approx(0.95)
    contract = next(
        b for b in ev.attribution_breakdown if b["defect"] == "contract_violation"
    )
    assert ev.attribution_confidence == pytest.approx(contract["attribution"])
    # start is a structural root: excluded, never a hidden origin that caps.
    assert ev.unknown_ancestors == []


def test_degraded_recovered_headline_confidence_equals_observation(mk):
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=_TERMINAL_OK))
    report = find_blame(inp)
    assert report.confidence == pytest.approx(report.evidence.observation_confidence)
    assert report.confidence == pytest.approx(0.95)


def test_degraded_recovered_has_no_manifestation(mk):
    """Nothing surfaced as a failure: the terminal is ok, so 'failure surfaced in
    output of render' must NOT be manufactured."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=_TERMINAL_OK))
    assert find_blame(inp).evidence.manifestation_run_ids == []


def test_contract_violation_surfaced_as_own_evidence_stream(mk):
    """The deterministic contract breach is its own structured evidence with
    provenance — NOT glued into the LLM judge_note (that gluing lived in the
    worker; here we assert the engine keeps the streams separate)."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=_TERMINAL_OK))
    ev = find_blame(inp).evidence

    assert ev.contract_violations == [
        {"run_id": "think", "agent": "think", "key": "file_type", "from": "docx", "to": "md"}
    ]
    # The judge note is preserved verbatim — no contract text spliced in.
    assert "contract" not in ev.judge_notes["think"].lower()


def test_contract_vs_terminal_note_flags_terminal_blind_spot(mk):
    """A contract breach + an ok terminal => the terminal judge is blind to the
    carried contract; say so, WITHOUT overclaiming propagation the payloads did
    not prove (the engine only observed the mid-pipeline rewrite — whether it
    reached the final artifact is settled by the worker's contract_propagation
    check, or out of band)."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=_TERMINAL_OK))
    report = find_blame(inp)
    cvt = note_of(report, "contract_vs_terminal")
    # The terminal-ok variant IS the "blind spot" statement: a breach exists and
    # the CONTENT judge still passed. The engine must not claim propagation it
    # did not observe — that variant carries no propagation evidence at all.
    assert cvt["variant"] == "terminal_ok"
    assert cvt["breaches"] == report.evidence.contract_violations


def test_recovered_verdict_reconciles_with_contract_caveat(mk):
    """#1: 'recovered' and 'breach' must not stand side by side unreconciled —
    the verdict itself says recovery is content-only and the run is not clean."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=_TERMINAL_OK))
    report = find_blame(inp)
    # The content-only caveat is not an optional sentence: it is carried by the
    # degraded_recovered note's own violation payload, so a recovered verdict
    # over a live breach cannot be rendered without it.
    assert note_of(report, "degraded_recovered")["violations"] == [
        {"key": "file_type", "from": "docx", "to": "md"}
    ]


def test_no_fabricated_drop_against_assumed_baseline(mk):
    """#3: think's only predecessor is an unscored structural root — there is no
    measured drop, so evidence.drops must NOT carry a '-0.85 from best-scored
    predecessor' computed against the assumed 1.00. The assumption lives,
    declared, in candidacy instead."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=_TERMINAL_OK))
    report = find_blame(inp)
    assert "think" not in report.evidence.drops
    # The attribution basis is stated: observed-intact input, not a fictional base.
    assert candidacy_of(report, "think")["violations"] == [
        {"key": "file_type", "from": "docx", "to": "md"}
    ]


def test_bad_terminal_keeps_it_a_cut_point(mk):
    """Same degraded node, but the terminal deliverable is bad -> the pipeline did
    NOT recover; this is a live cut_point, not a near-miss."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")],
                         flags=["missing_required_content"], terminal=_TERMINAL_BAD))
    report = find_blame(inp)
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["think"]
    # Headline for a real cut_point is attribution; observation still recorded.
    assert report.confidence == pytest.approx(report.evidence.attribution_confidence)
    assert report.evidence.observation_confidence == pytest.approx(0.95)


def test_no_terminal_verdict_is_not_degraded_recovered(mk):
    """Without terminal ground truth we cannot claim the run recovered, so a
    degraded boundary stays a cut_point (honest: we don't know the deliverable is
    fine)."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=None))
    assert find_blame(inp).report_type == "cut_point"


def test_mild_recovered_degradation_without_deterministic_signal(mk):
    """A mild degradation (no contract breach, no content flag) that recovered:
    observation is severity-based (not the 0.95 deterministic pin), still
    degraded_recovered."""
    inp = mk(**_pipeline(0.3, terminal=_TERMINAL_OK))
    report = find_blame(inp)
    assert report.report_type == "degraded_recovered"
    # severity = (0.5 - 0.3) / 0.5 = 0.4
    assert report.evidence.observation_confidence == pytest.approx(0.4)
    assert report.confidence == pytest.approx(0.4)


def test_terminal_evidence_carries_caveat_when_breach_and_ok(mk):
    """#2 (round 3): the terminal section is the loudest element of the report —
    an unqualified 'ok 1.00' above a proven mid-pipeline breach lies by omission.
    The engine qualifies the verdict AT the verdict (the worker upgrades the
    wording once the payload check settles propagation)."""
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=_TERMINAL_OK))
    tv = find_blame(inp).evidence.terminal_verdict
    assert tv is not None
    assert "ok in CONTENT only" in tv["caveat"]
    assert "file_type" in tv["caveat"]

    # No breach -> no caveat key is set at all (an unqualified ok stays honest).
    clean = mk(**_pipeline(0.3, terminal=_TERMINAL_OK))
    tv_clean = find_blame(clean).evidence.terminal_verdict
    assert tv_clean is not None
    assert "caveat" not in tv_clean


def test_boundary_attribution_capped_without_contract_evidence(mk):
    """An origin at the observability boundary WITHOUT the observed-input
    contract evidence gets the hard 0.6 ceiling: it is the origin partly
    because it is the first node we could see."""
    inp = mk(**_pipeline(0.10, flags=["missing_required_content"], terminal=_TERMINAL_BAD))
    report = find_blame(inp)
    assert report.report_type == "cut_point"
    assert report.evidence.attribution_confidence == pytest.approx(0.6)
    capped = note_of(report, "attribution_capped")
    assert capped is not None and capped["cap"] == pytest.approx(0.6)


def test_observed_predecessor_attribution_not_capped(mk):
    """A REAL measured drop from a scored predecessor keeps its attribution —
    the cap is strictly about assumed baselines."""
    inp = mk(
        nodes=["a", "b", "c"],
        edges=[("a", "b"), ("b", "c")],
        scores={"a": 1.0, "b": 1.0, "c": 0.1},
    )
    report = find_blame(inp)
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["c"]
    assert report.confidence > 0.8  # measured drop, no boundary cap
    assert note_of(report, "attribution_capped") is None


def test_stale_terminal_suppresses_manifestation(mk):
    """A STALE terminal's failure claim was discarded — with no live failure
    evidence, 'failure surfaced in output of X' must not render."""
    stale_bad = TerminalVerdict(
        bad=True, score=0.0, reasoning="old deterministic failure",
        checkable=False, stale=True,
    )
    inp = mk(**_pipeline(0.15, contract=[("file_type", "docx", "md")], terminal=stale_bad))
    report = find_blame(inp)
    assert report.report_type == "cut_point"  # contract fault is still live
    assert report.evidence.manifestation_run_ids == []
    assert note_of(report, "terminal_stale") is not None
