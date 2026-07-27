"""Verdict-level golden fixtures across the via matrix (channel decoupling).

These are ENGINE-level fixtures with HAND-AUTHORED ground truth (the scenario is
constructed, the expected verdict/inventory written from first principles — never
lifted from an engine verdict). They replace the old implementation tests that
asserted the sentinel constants (0.15 / 0.6).

They are NOT the real-trace corpus: regenerating the four real traces needs the
live judge (an API call), deferred. What they lock is the engine's verdict logic
per channel: which node is the origin, via which channel, carrying which defects,
and — the false-positive guard — that a clean run accuses no one.
"""

from blame_engine import (
    NodeScore,
    TerminalVerdict,
    find_blame,
    select_candidates,
)


def _ns(run_id, score, *, contract=(), flawed=None, flags=(), signals=()):
    return NodeScore(
        run_id=run_id,
        score=score,
        components={"judge": score} if score is not None else {},
        input_flawed=flawed,
        unscored_reason="payload_missing" if score is None else None,
        judge_note=None,
        flags=tuple(flags),
        contract_violations=tuple(contract),
        deterministic_signals=tuple(signals),
    )


def _ok_terminal():
    return TerminalVerdict(bad=False, score=1.0, reasoning="content complete", checkable=True)


def _bad_terminal():
    return TerminalVerdict(bad=True, score=0.0, reasoning="content is wrong", checkable=True)


def _defects(report):
    return {d["defect"] for d in report.evidence.attribution_breakdown}


def _via(inp, run_id):
    return next(c.via for c in select_candidates(inp) if c.run_id == run_id)


def test_via_matrix_deterministic_only(mk):
    """A judge-healthy node (0.9) with a contract breach, successors healthy,
    terminal ok. Ground truth: ONE fault — the contract breach — localised via the
    deterministic channel, no content_degradation, near-certain attribution.
    (Engine verdict is degraded_recovered; the worker's propagation check is what
    escalates it to shipped_with_latent_defect — out of the engine's scope.)"""
    inp = mk(
        nodes=["orchestrator", "think", "render"],
        edges=[("orchestrator", "think"), ("think", "render")],
        scores={
            "orchestrator": _ns("orchestrator", 1.0),
            "think": _ns("think", 0.9, contract=[("file_type", "docx", "md")]),
            "render": _ns("render", 1.0),
        },
        terminal_verdict=_ok_terminal(),
    )
    report = find_blame(inp)

    assert report.report_type == "degraded_recovered"
    assert report.culprit_run_ids == ["think"]
    assert _via(inp, "think") == "deterministic"
    assert _defects(report) == {"contract_violation"}          # NO content_degradation
    assert report.evidence.attribution_confidence == 0.95


def test_via_matrix_content_only(mk):
    """A genuine content drop (1.0 -> 0.2) with NO deterministic fault, terminal
    bad. Ground truth: ONE fault — content_degradation — localised via the content
    channel as a cut_point, no contract_violation defect."""
    inp = mk(
        nodes=["orchestrator", "writer"],
        edges=[("orchestrator", "writer")],
        scores={
            "orchestrator": _ns("orchestrator", 1.0),
            "writer": _ns("writer", 0.2),
        },
        terminal_verdict=_bad_terminal(),
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["writer"]
    assert _via(inp, "writer") == "content"
    assert _defects(report) == {"content_degradation"}          # NO contract_violation


def test_via_matrix_both(mk):
    """One node with BOTH a real content drop (1.0 -> 0.2, measured predecessor)
    AND a contract breach. Ground truth: ONE origin (via=both) carrying TWO defects
    — contract near-certain, content_degradation measured (not the boundary cap,
    because the predecessor was scored)."""
    inp = mk(
        nodes=["orchestrator", "think", "render"],
        edges=[("orchestrator", "think"), ("think", "render")],
        scores={
            "orchestrator": _ns("orchestrator", 1.0),
            "think": _ns("think", 0.2, contract=[("file_type", "docx", "md")]),
            "render": _ns("render", 1.0),
        },
        terminal_verdict=_ok_terminal(),
    )
    report = find_blame(inp)

    assert report.culprit_run_ids == ["think"]
    assert _via(inp, "think") == "both"
    assert _defects(report) == {"contract_violation", "content_degradation"}
    # Headline attribution is the contract breach (the verdict-carrying defect).
    assert report.evidence.attribution_confidence == 0.95


def test_via_matrix_clean_accuses_no_one(mk):
    """FALSE-POSITIVE GUARD — the most important fixture in the set. Everything
    healthy, terminal ok. A detective that finds a crime everywhere is worse than
    none: there must be NO culprit and NO localised defect."""
    inp = mk(
        nodes=["orchestrator", "writer"],
        edges=[("orchestrator", "writer")],
        scores={
            "orchestrator": _ns("orchestrator", 1.0),
            "writer": _ns("writer", 1.0),
        },
        terminal_verdict=_ok_terminal(),
    )
    report = find_blame(inp)

    assert report.culprit_run_ids == []
    assert report.report_type == "unclassified"
    assert select_candidates(inp) == []
    assert report.evidence.attribution_breakdown == []
