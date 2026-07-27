"""Verdict-level golden fixtures for the §3 derivation rows the via-matrix and
terminal-split sets did NOT already cover: verification_gap (both bases),
composition_failure, root_cause_external, multi_culprit.

Hand-authored ground truth (never lifted from an engine verdict): each scenario
is constructed from first principles and the expected verdict/inventory written
down BEFORE the assertion. In addition to the legacy verdict, every fixture
checks the schema-2 PROJECTION round-trips: `derive_report_type` over the typed
`Defect[]` stored in evidence reproduces the same report_type — locking the
single-mapping property (§2.3, F1.4).
"""

from blame_engine import (
    NodeScore,
    TerminalVerdict,
    derive_report_type,
    deserialize_defect,
    find_blame,
)


def _ns(run_id, score, *, contract=(), flawed=None, flags=()):
    return NodeScore(
        run_id=run_id,
        score=score,
        components={"judge": score} if score is not None else {},
        input_flawed=flawed,
        unscored_reason="payload_missing" if score is None else None,
        judge_note=None,
        flags=tuple(flags),
        contract_violations=tuple(contract),
    )


def _derived(report):
    """report_type as recovered from the schema-2 defects — must equal the legacy
    report_type on every §3-covered fixture."""
    return derive_report_type(
        [deserialize_defect(d) for d in report.evidence.defects]
    )


def _ok():
    return TerminalVerdict(bad=False, score=1.0, reasoning="deliverable is good", checkable=True)


def _bad():
    return TerminalVerdict(bad=True, score=0.1, reasoning="the deliverable is empty", checkable=True)


# --- verification_gap ----------------------------------------------------


def test_verification_gap_passed_bad_terminal(mk):
    """Every producer AND every verifier scored healthy, yet the terminal
    deliverable is bad ground truth. Ground truth: the verifiers that let bad
    work through are the failure (passed_bad_terminal). No producer localises, so
    the verdict is verification_gap at the verifiers, capped at 0.6."""
    inp = mk(
        nodes=["start", "writer", "qa", "eval"],
        edges=[("start", "writer"), ("writer", "qa"), ("qa", "eval")],
        scores={
            "start": _ns("start", None),
            "writer": _ns("writer", 0.95),
            "qa": _ns("qa", 0.95, flags=["issued_pass"]),
            "eval": _ns("eval", 0.95, flags=["issued_pass"]),
        },
        terminal_verdict=_bad(),
    )
    report = find_blame(inp)

    assert report.report_type == "verification_gap"
    assert set(report.culprit_run_ids) == {"qa", "eval"}
    assert report.confidence == 0.6
    bases = {g["basis"] for g in report.evidence.verification_gaps}
    assert bases == {"passed_bad_terminal"}
    assert _derived(report) == "verification_gap"


def test_verification_gap_wrong_fail_false_alarm(mk):
    """A verifier issued FAIL and the role-aware judge scored that verdict wrong
    (0.27 < threshold) while the terminal is ok ground truth — a false alarm. The
    FAIL verifier believed its input flawed, so it is excluded from cut-point
    candidacy and the graph reaches the gap upgrade. Ground truth:
    verification_gap (verdict_scored_incorrect), not a cut_point on the verifier."""
    inp = mk(
        nodes=["gen", "review"],
        edges=[("gen", "review")],
        scores={
            "gen": _ns("gen", 0.9),
            "review": _ns("review", 0.27, flawed=True, flags=["issued_fail"]),
        },
        terminal_verdict=_ok(),
    )
    report = find_blame(inp)

    assert report.report_type == "verification_gap"
    assert report.culprit_run_ids == ["review"]
    assert report.confidence == 0.6
    gaps = {g["run_id"]: g["basis"] for g in report.evidence.verification_gaps}
    assert gaps == {"review": "verdict_scored_incorrect"}
    assert _derived(report) == "verification_gap"


# --- composition_failure -------------------------------------------------


def test_composition_failure_all_healthy_bad_terminal(mk):
    """Every node individually scored healthy (no drops, no unknowns) and there is
    NO verifier to retroactively blame, yet the terminal deliverable is bad. The
    fault cannot be localised to any node — it enters at the orchestration layer.
    Ground truth: composition_failure pointing at the orchestration entry (the
    source super-node), capped at 0.4 (a suspect, not a proven culprit); the
    content defect is Unlocalized by construction."""
    inp = mk(
        nodes=["start", "writer", "compose"],
        edges=[("start", "writer"), ("writer", "compose")],
        scores={
            "start": _ns("start", None),
            "writer": _ns("writer", 0.95),
            "compose": _ns("compose", 0.95),
        },
        terminal_verdict=_bad(),
    )
    report = find_blame(inp)

    assert report.report_type == "composition_failure"
    assert report.confidence == 0.4
    # The source/orchestrator entry is the suspect (start is the graph source).
    assert report.culprit_run_ids == ["start"]
    assert _derived(report) == "composition_failure"


# --- root_cause_external -------------------------------------------------


def test_root_cause_external_flawed_source(mk):
    """The graph's source reports its OWN input was already flawed (input_flawed)
    and no in-graph node qualifies as an origin (the source is excluded, its
    successor merely inherited the low quality). Ground truth: the fault entered
    from outside the observed graph — root_cause_external, capped at 0.5."""
    inp = mk(
        nodes=["ingest", "worker"],
        edges=[("ingest", "worker")],
        scores={
            "ingest": _ns("ingest", 0.3, flawed=True),
            "worker": _ns("worker", 0.3),
        },
        terminal_verdict=_bad(),
    )
    report = find_blame(inp)

    assert report.report_type == "root_cause_external"
    assert report.culprit_run_ids == ["ingest"]
    assert report.confidence <= 0.5
    assert _derived(report) == "root_cause_external"


# --- multi_culprit -------------------------------------------------------


def test_multi_culprit_two_independent_content_origins(mk):
    """Two independent branches each drop from a healthy shared source (1.0 -> 0.2)
    — two origins with no ancestry between them. Ground truth: multi_culprit,
    both nodes blamed, headline confidence capped at 0.8 (a blend never sold as
    certainty)."""
    inp = mk(
        nodes=["source", "a", "b"],
        edges=[("source", "a"), ("source", "b")],
        scores={
            "source": _ns("source", 1.0),
            "a": _ns("a", 0.2),
            "b": _ns("b", 0.2),
        },
        terminal_verdict=_bad(),
    )
    report = find_blame(inp)

    assert report.report_type == "multi_culprit"
    assert set(report.culprit_run_ids) == {"a", "b"}
    assert report.confidence <= 0.8
    assert _derived(report) == "multi_culprit"


# --- projection round-trips on the pre-existing golden fixtures too ------


def test_derive_matches_on_clean_and_cut_point(mk):
    """The single-mapping property also holds for the simplest rows: a clean run
    derives to unclassified (no defects), a real content drop to cut_point."""
    clean = find_blame(
        mk(
            nodes=["o", "w"],
            edges=[("o", "w")],
            scores={"o": _ns("o", 1.0), "w": _ns("w", 1.0)},
            terminal_verdict=_ok(),
        )
    )
    assert clean.report_type == "unclassified"
    assert _derived(clean) == "unclassified"

    cut = find_blame(
        mk(
            nodes=["o", "w"],
            edges=[("o", "w")],
            scores={"o": _ns("o", 1.0), "w": _ns("w", 0.2)},
            terminal_verdict=_bad(),
        )
    )
    assert cut.report_type == "cut_point"
    assert _derived(cut) == "cut_point"


def test_content_only_degraded_recovered_derives_not_unclassified(mk):
    """§3 gap fix: a mild recovered content drop (no contract breach), terminal
    ok. Ground truth: degraded_recovered — a fragile node the pipeline
    compensated for, NOT unclassified and NOT a cut_point."""
    from blame_engine import NodeScore, TerminalVerdict, derive_report_type

    def _ns(run_id, score):
        return NodeScore(
            run_id=run_id, score=score, components={"judge": score} if score is not None else {},
            input_flawed=None, unscored_reason="payload_missing" if score is None else None,
            judge_note=None,
        )

    inp = mk(
        nodes=["orchestrator", "writer", "polish"],
        edges=[("orchestrator", "writer"), ("writer", "polish")],
        scores={
            "orchestrator": _ns("orchestrator", 1.0),
            "writer": _ns("writer", 0.4),   # dropped here...
            "polish": _ns("polish", 1.0),   # ...but recovered downstream
        },
        terminal_verdict=TerminalVerdict(bad=False, score=1.0, reasoning="ok", checkable=True),
    )
    report = find_blame(inp)

    assert report.report_type == "degraded_recovered"
    # The projection now emits a RECOVERED content defect (not zero defects),
    # so derive_report_type round-trips instead of returning unclassified.
    from blame_engine import deserialize_defect
    defects = [deserialize_defect(d) for d in report.evidence.defects]
    assert any(d.kind == "content" and d.recovered for d in defects)
    assert derive_report_type(defects) == "degraded_recovered"
