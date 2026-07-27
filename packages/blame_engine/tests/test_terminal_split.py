"""Verdict-level golden fixtures for the terminal rubric split.

Hand-authored ground truth (never lifted from an engine verdict), written from
the 2026-07-24 corpus annotations in night_run.md BEFORE the engine change:

- Run C shape: a deterministic-only origin (content judged healthy, successors
  recovered) with a content-bad terminal used to produce a cut_point whose own
  evidence showed zero content candidates — a verdict claiming a defect its
  evidence does not show. Ground truth: the contract fault is localized, the
  content defect is observed at the terminal but NOT localized.
- Run A/B shape: with the terminal verdict split into content vs form, a
  format-only miss no longer flips the terminal to content-bad, so the
  recovered deterministic origin stays degraded_recovered (one fault) and no
  verification gap is manufactured on verifiers that passed good content.
- Requirement provenance: a deterministic contract reference that does not
  appear in the judge's verbatim requirement quote is scaffold, not the user's
  ask — the report must say so instead of printing both side by side.
"""

from blame_engine import NodeScore, TerminalVerdict, find_blame, select_candidates
from conftest import note_of


def _ns(run_id, score, *, contract=(), flags=()):
    return NodeScore(
        run_id=run_id,
        score=score,
        components={"judge": score} if score is not None else {},
        input_flawed=None,
        unscored_reason="payload_missing" if score is None else None,
        judge_note=None,
        flags=tuple(flags),
        contract_violations=tuple(contract),
    )


def _defects(report):
    return {d["defect"] for d in report.evidence.attribution_breakdown}


def _note(report, slug):
    """The TYPED note record for ``slug`` (its payload), or None.

    Asserted on the payload, never the sentence: what matters is that the note
    fired with the right evidence in it, not how the template words it.
    """
    return note_of(report, slug)


def test_content_bad_terminal_with_only_deterministic_origin_is_unlocalized(mk):
    """Run C shape. Terminal CONTENT is bad, but the sole candidate is a
    deterministic-channel origin whose content the judge scored healthy and
    whose successors recovered. Ground truth: NOT a cut_point (no content
    candidate exists); the contract fault is localized at think, the content
    defect is observed-but-unlocalized, and the headline confidence must NOT
    be the contract attribution."""
    inp = mk(
        nodes=["start", "think", "act", "render"],
        edges=[("start", "think"), ("think", "act"), ("act", "render")],
        scores={
            "start": _ns("start", None),
            "think": _ns("think", 0.56, contract=[("file_type", "docx", "md")]),
            "act": _ns("act", 0.93),
            "render": _ns("render", 1.0),
        },
        terminal_verdict=TerminalVerdict(
            bad=True,
            score=0.4,
            reasoning="lacks specific financial figures",
            checkable=True,
        ),
    )
    report = find_blame(inp)

    assert report.report_type == "terminal_defect_unlocalized"
    assert report.culprit_run_ids == ["think"]
    # The attribution shown is the CONTRACT fault only — no content row exists
    # for a deterministic-only origin, so nothing can be misread as terminal
    # content blame.
    assert _defects(report) == {"contract_violation"}
    assert report.evidence.attribution_confidence == 0.95
    # Headline is the unlocalized terminal observation, capped — not 0.95.
    assert report.confidence == 0.6
    # The content defect is observed but NOT localized: only the unlocalized
    # note kind can say that, and it carries the terminal's own reasoning.
    note = _note(report, "terminal_defect_unlocalized")
    assert note is not None
    assert note["agent"] == "think"
    assert note["violations"] == [{"key": "file_type", "from": "docx", "to": "md"}]


def test_form_only_miss_stays_degraded_recovered_with_design_note(mk):
    """Run A/B shape after the split. Content terminal is ok; the FORM
    dimension is bad (md shipped where PDF was asked). Ground truth: ONE fault
    (the contract breach, degraded_recovered at engine level), NO verification
    gap on the passing verifiers, and a design-level form_defect_shipped note
    replaces them ("no verifier owns form/contract vision")."""
    inp = mk(
        nodes=["start", "think", "act", "qa"],
        edges=[("start", "think"), ("think", "act"), ("act", "qa")],
        scores={
            "start": _ns("start", None),
            "think": _ns("think", 0.56, contract=[("file_type", "docx", "md")]),
            "act": _ns("act", 0.93),
            "qa": _ns("qa", 0.93),
        },
        terminal_verdict=TerminalVerdict(
            bad=False,
            score=1.0,
            reasoning="content complete and correct",
            checkable=True,
            form_bad=True,
            form_requirement="jako PDF",
            form_observed="markdown text",
        ),
    )
    report = find_blame(inp)

    assert report.report_type == "degraded_recovered"
    assert report.culprit_run_ids == ["think"]
    assert _defects(report) == {"contract_violation"}
    # No verifier gap and no claimed->effective override on qa: the terminal
    # content is ok, so there is no bad terminal to refute qa's PASS with.
    assert report.evidence.score_overrides == []
    assert _note(report, "verification_gap") is None
    # The DESIGN-level framing is the form_defect_shipped kind itself; the
    # requirement is quoted verbatim from the initial input.
    design = _note(report, "form_defect_shipped")
    assert design is not None
    assert design["requirement"] == "jako PDF"


def test_requirement_provenance_flags_scaffold_reference(mk):
    """The deterministic contract reference ('docx') does not appear in the
    judge's verbatim requirement quote ('jako PDF') — the report must mark the
    reference as scaffold provenance instead of printing both side by side."""
    inp = mk(
        nodes=["start", "think", "act"],
        edges=[("start", "think"), ("think", "act")],
        scores={
            "start": _ns("start", None),
            "think": _ns("think", 0.56, contract=[("file_type", "docx", "md")]),
            "act": _ns("act", 0.93),
        },
        terminal_verdict=TerminalVerdict(
            bad=False,
            score=1.0,
            reasoning="content complete",
            checkable=True,
            form_bad=True,
            form_requirement="jako PDF",
        ),
    )
    report = find_blame(inp)

    # The scaffold verdict IS the requirement_provenance kind: it exists only
    # when the contract reference is absent from the verbatim requirement quote.
    note = _note(report, "requirement_provenance")
    assert note is not None
    assert note["from"] == "docx" and note["requirement"] == "jako PDF"


def test_requirement_provenance_silent_when_reference_matches_quote(mk):
    """Negative guard: a contract reference that DOES appear in the requirement
    quote ('pdf' in 'jako PDF') is user-request-derived — no provenance note."""
    inp = mk(
        nodes=["start", "think", "act"],
        edges=[("start", "think"), ("think", "act")],
        scores={
            "start": _ns("start", None),
            "think": _ns("think", 0.56, contract=[("file_type", "pdf", "md")]),
            "act": _ns("act", 0.93),
        },
        terminal_verdict=TerminalVerdict(
            bad=False,
            score=1.0,
            reasoning="content complete",
            checkable=True,
            form_bad=True,
            form_requirement="jako PDF",
        ),
    )
    report = find_blame(inp)

    assert _note(report, "requirement_provenance") is None


def test_real_content_origin_still_wins_cut_point(mk):
    """Guard: the unlocalized outcome must NOT swallow genuine content
    failures. A real content drop (1.0 -> 0.2) with a bad terminal stays a
    cut_point with a content candidate — the split changes nothing here."""
    inp = mk(
        nodes=["start", "writer"],
        edges=[("start", "writer")],
        scores={
            "start": _ns("start", 1.0),
            "writer": _ns("writer", 0.2),
        },
        terminal_verdict=TerminalVerdict(
            bad=True, score=0.1, reasoning="content is wrong", checkable=True
        ),
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["writer"]
    assert next(c.via for c in select_candidates(inp) if c.run_id == "writer") == "content"
