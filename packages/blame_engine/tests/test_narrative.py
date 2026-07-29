"""The narrative layer: the ONE place prose is covered (verdict refactor §2.4).

Behaviour tests assert typed records (slug/verdict + payload); this file asserts
the templates that turn them into English. The split is the point: rewording a
sentence touches exactly one test, and a template that drifts away from the data
it claims to describe fails here instead of nowhere.

The invariants locked below:

1. **Every emitted record has a template.** ``render_note``/``render_candidacy``
   degrade to the bare slug for an unknown kind, so an emitter added without a
   template is silently ugly — this catches it.
2. **A template interpolates ONLY its own record's fields.** There is no
   free-prose channel from decision code, so a sentence claiming a defect must
   hold the data that shows it. Enforced by construction (the templates take a
   Mapping and nothing else) and checked here on the record→sentence round trip
   through JSON, which a stored report replays.
3. **Certainty taxonomy**: "ground truth" is banned outright; deterministic
   findings render as "deterministic", judged ones as "assessment" (§2.4).
4. **Caveats are unclippable chips**, never mid-sentence prose (§11 row 16).
"""

import json

import pytest

from blame_engine import (
    PROVENANCE_LABELS,
    REASON_INPUT_ALREADY_FLAWED,
    REASON_NO_CONTENT_CANDIDATE,
    REASON_NO_FORM_VERIFIER,
    REASON_ORCHESTRATION_LAYER,
    CandidacyRecord,
    Defect,
    Design,
    Finding,
    Localized,
    NodeScore,
    NoteRecord,
    RuleFingerprint,
    TerminalVerdict,
    Unlocalized,
    deserialize_finding,
    deserialize_note,
    find_blame,
    origin_reason_phrase,
    render_candidacy,
    render_defect,
    render_finding,
    render_note,
    render_score_override_reason,
    serialize_note,
)
from blame_engine.narrative import _CANDIDACY_TEMPLATES, _NOTE_TEMPLATES

BAD = TerminalVerdict(bad=True, score=0.2, reasoning="the report is empty")
OK = TerminalVerdict(bad=True and False, score=1.0, reasoning="looks complete")


def _ns(run_id, score, *, contract=(), flags=(), unscored=None, signals=()):
    return NodeScore(
        run_id=run_id,
        score=score,
        components={"judge": score} if score is not None else {},
        input_flawed=None,
        unscored_reason=unscored,
        judge_note=None,
        flags=tuple(flags),
        contract_violations=tuple(contract),
        deterministic_signals=tuple(signals),
    )


_EMPTY_OUTPUT_SIGNAL = {
    "name": "empty_output",
    "severity": "fail",
    "code": "empty_output_with_spend",
    "params": {"tokens_out": 1200, "chars": 0},
    "detail": "produced no output while spending 1200 output tokens",
    "basis": "output.value recorded and empty (0 chars); gen_ai.usage.output_tokens=1200",
}


def _scenarios(mk):
    """Graphs that between them exercise every cascade row and candidacy branch.

    Deliberately built from the shapes the corpus actually produced (a contract
    breach behind an ok terminal, a fabrication cascade, a cycle, a verifier
    gap, an unscored node, a disconnected graph), not from template names — the
    coverage assertion below is only meaningful if the inputs are real.
    """
    return [
        # 1. clean / unclassified
        mk(nodes=["a", "b"], edges=[("a", "b")], scores={"a": 0.9, "b": 0.95}),
        # 2. all scores unknown
        mk(nodes=["a", "b"], edges=[("a", "b")]),
        # 3. cut_point at a measured drop, bad terminal, verifier gap, cascade
        mk(
            nodes=["think", "act", "render", "qa"],
            edges=[("think", "act"), ("act", "render"), ("render", "qa")],
            scores={
                "think": _ns("think", 0.67, flags=["missing_required_content"]),
                "act": _ns("act", 0.93),
                "render": _ns("render", 0.93),
                "qa": _ns("qa", 0.93),
            },
            agent_names={"qa": "qa"},
            terminal_verdict=BAD,
        ),
        # 4. degraded_recovered over a contract breach behind an ok terminal,
        #    with the form dimension bad (design gap + requirement provenance)
        mk(
            nodes=["start", "think", "act"],
            edges=[("start", "think"), ("think", "act")],
            scores={
                "start": _ns("start", None, unscored="payload_missing"),
                "think": _ns("think", 0.56, contract=[("file_type", "docx", "md")]),
                "act": _ns("act", 0.93),
            },
            terminal_verdict=TerminalVerdict(
                bad=False, score=1.0, reasoning="content complete", checkable=True,
                form_bad=True, form_requirement="jako PDF",
                form_observed="markdown text",
            ),
        ),
        # 5. cycle: blame drills into the worst member
        mk(
            nodes=["orch", "w1", "w2", "final"],
            edges=[("orch", "w1"), ("w1", "orch"), ("orch", "w2"),
                   ("w2", "orch"), ("orch", "final")],
            scores={"orch": 0.9, "w1": 0.9, "w2": 0.1, "final": 0.4},
            end_times={"orch": 9.0, "w1": 1.0, "w2": 2.0, "final": 10.0},
            terminal_verdict=BAD,
        ),
        # 6. composition failure (everyone healthy, terminal bad)
        mk(
            nodes=["o", "a", "b"],
            edges=[("o", "a"), ("a", "b")],
            scores={"o": 0.9, "a": 0.85, "b": 0.8},
            terminal_verdict=BAD,
        ),
        # 7. disconnected graph (topology warning) + an unscored non-root
        mk(
            nodes=["a", "b", "mid", "c"],
            edges=[("a", "b"), ("mid", "c")],
            scores={
                "a": 0.9, "b": 0.2,
                "mid": _ns("mid", None, unscored="payload_missing"), "c": 0.9,
            },
        ),
        # 8. root_cause_external: the source's own judge says its input was flawed
        mk(
            nodes=["src", "out"],
            edges=[("src", "out")],
            scores={
                "src": NodeScore(
                    run_id="src", score=0.3, components={}, input_flawed=True,
                    unscored_reason=None, judge_note=None,
                ),
                "out": _ns("out", 0.3),
            },
            terminal_verdict=BAD,
        ),
        # 9. not-checkable terminal (discarded as ground truth)
        mk(
            nodes=["a", "b"],
            edges=[("a", "b")],
            scores={"a": 0.9, "b": 0.9},
            terminal_verdict=TerminalVerdict(
                bad=True, score=0.0, reasoning="empty", checkable=False
            ),
        ),
        # 10. stale terminal (its deterministic basis stopped reproducing)
        mk(
            nodes=["a", "b"],
            edges=[("a", "b")],
            scores={"a": 0.9, "b": 0.2},
            terminal_verdict=TerminalVerdict(
                bad=True, score=0.0, reasoning="old failure",
                checkable=False, stale=True,
            ),
        ),
        # 11. verification_gap: nothing localized, but a verifier passed bad work
        mk(
            nodes=["gen", "qa", "eval"],
            edges=[("gen", "qa"), ("qa", "eval")],
            scores={"gen": 0.9, "qa": 0.92, "eval": 0.93},
            agent_names={"gen": "gen", "qa": "qa", "eval": "eval"},
            terminal_verdict=BAD,
        ),
        # 12. deterministic origin, content-bad terminal, successors healthy:
        #     the contract fault localizes, the content defect does not
        mk(
            nodes=["start", "think", "act", "render"],
            edges=[("start", "think"), ("think", "act"), ("act", "render")],
            scores={
                "start": _ns("start", None, unscored="payload_missing"),
                "think": _ns("think", 0.9, contract=[("file_type", "docx", "md")]),
                "act": _ns("act", 0.93),
                "render": _ns("render", 0.95),
            },
            terminal_verdict=BAD,
        ),
        # 13. a sub-threshold node NOT downstream of the localized origin —
        #     "an independent low", never "shadowed by the origin upstream"
        mk(
            nodes=["a", "b", "c", "d"],
            edges=[("a", "b"), ("c", "d")],
            scores={
                "a": 0.9,
                "b": 0.2,
                "c": NodeScore(
                    run_id="c", score=0.3, components={}, input_flawed=True,
                    unscored_reason=None, judge_note=None,
                ),
                "d": 0.9,
            },
            terminal_verdict=BAD,
        ),
        # 14. below threshold but NOT an origin, with nothing localized at all:
        #     the trace must not point the reader upstream at nothing
        mk(
            nodes=["p1", "p2", "p3", "out"],
            edges=[("p1", "p2"), ("p2", "p1"), ("p2", "p3"), ("p3", "p2"),
                   ("p3", "out")],
            scores={"p1": 0.3, "p2": 0.35, "p3": 0.85, "out": 0.85},
            end_times={"p1": 1.0, "p2": 2.0, "p3": 3.0, "out": 4.0},
            terminal_verdict=BAD,
        ),
        # 15. a node that recorded an EMPTY output while its usage says it spent
        #     tokens: unscored, but the emptiness is evidence against the node,
        #     not an instrumentation blind spot (a separate note from
        #     instrumentation_warning, which scenario 7 covers)
        mk(
            nodes=["draft", "review", "ship"],
            edges=[("draft", "review"), ("review", "ship")],
            scores={
                "draft": _ns("draft", 0.9),
                "review": _ns(
                    "review", None, unscored="empty_output",
                    flags=["empty_output"], signals=[_EMPTY_OUTPUT_SIGNAL],
                ),
                "ship": _ns("ship", 0.9),
            },
            terminal_verdict=BAD,
        ),
    ]


def _all_records(mk):
    notes, candidacies = [], []
    for inp in _scenarios(mk):
        ev = find_blame(inp).evidence
        notes.extend(ev.note_records)
        candidacies.extend(ev.candidacy_records.values())
    return notes, candidacies


# --- 1. every emitted record renders through a template ------------------


def test_every_emitted_note_has_a_template(mk):
    notes, _ = _all_records(mk)
    assert notes, "the scenario set emitted no notes at all"
    for rec in notes:
        assert rec["slug"] in _NOTE_TEMPLATES, f"no template for note {rec['slug']!r}"
        rendered = render_note(deserialize_note(rec))
        # A template that fell through would return the bare slug.
        assert rendered != rec["slug"]
        assert rendered.startswith(rec["slug"]), (
            f"{rec['slug']!r} must lead its own sentence — the slug is the "
            f"prefix stored reports and the legacy renderer key off"
        )


def test_every_emitted_candidacy_has_a_template(mk):
    _, candidacies = _all_records(mk)
    assert candidacies
    for rec in candidacies:
        assert rec["verdict"] in _CANDIDACY_TEMPLATES, (
            f"no template for candidacy verdict {rec['verdict']!r}"
        )
        rendered = render_candidacy(
            CandidacyRecord(rec["verdict"], rec["data"])
        )
        assert rendered != rec["verdict"] and rendered


def test_scenarios_cover_the_note_and_candidacy_tables(mk):
    """The template tables and the scenario set must not drift apart: a template
    nothing can emit is dead prose, and an emitter with no scenario is untested
    prose. Both directions are failures worth seeing."""
    notes, candidacies = _all_records(mk)
    seen_notes = {r["slug"] for r in notes}
    seen_verdicts = {r["verdict"] for r in candidacies}

    # Emitted by paths this suite exercises elsewhere (worker-fed escalation,
    # multi-origin rows, loop anomalies) rather than by the scenarios above.
    covered_elsewhere = {
        "escalation", "multi_culprit", "loop_detected", "independent_origins",
        "terminal_defect_unlocalized", "attribution_capped", "no_scores",
    }
    assert set(_NOTE_TEMPLATES) - seen_notes <= covered_elsewhere

    candidacy_elsewhere = {
        "origin_escalated", "loop_member", "origin_boundary",
        "origin_by_classification", "gap_verdict_scored_incorrect",
        "degradation_path_start", "degradation_path_member", "whistleblower",
        "origin_cumulative", "transient_low", "origin_vs_predecessor",
    }
    assert set(_CANDIDACY_TEMPLATES) - seen_verdicts <= candidacy_elsewhere


# --- 2. record -> sentence survives the JSON round trip ------------------


def test_notes_render_identically_after_a_json_round_trip(mk):
    """A stored schema-2 report replays its own rationale. If a template read
    anything but its record's (JSON-safe) payload, the replay would diverge."""
    for inp in _scenarios(mk):
        ev = find_blame(inp).evidence
        replayed = [
            render_note(deserialize_note(json.loads(json.dumps(rec))))
            for rec in ev.note_records
        ]
        assert replayed == ev.notes


def test_candidacy_renders_identically_after_a_json_round_trip(mk):
    for inp in _scenarios(mk):
        ev = find_blame(inp).evidence
        replayed = {
            run_id: render_candidacy(
                CandidacyRecord(**json.loads(json.dumps(rec)))
            )
            for run_id, rec in ev.candidacy_records.items()
        }
        assert replayed == ev.candidacy


def test_unknown_slug_degrades_instead_of_inventing_interpretation():
    assert render_note(NoteRecord("brand_new_kind", {"x": 1})) == "brand_new_kind"
    assert render_candidacy(CandidacyRecord("brand_new_verdict")) == (
        "brand_new_verdict"
    )
    assert serialize_note(NoteRecord("k", {"a": 1})) == {
        "slug": "k", "data": {"a": 1}
    }


# --- 3. certainty taxonomy: "ground truth" is banned ---------------------


def test_no_template_calls_anything_ground_truth(mk):
    """"Ground truth" overclaimed and is banned outright (§11 row 11).

    It labelled the tier1 TERMINAL verdict — an LLM judgment — as beyond
    dispute, and it collided with the UI's human-feedback label. Deterministic
    findings say "deterministic", judged ones say "assessment", and the terminal
    is quoted as a "checkable assessment", which is exactly what it is.
    """
    notes, candidacies = _all_records(mk)
    for rec in notes:
        rendered = render_note(deserialize_note(rec))
        assert "ground truth" not in rendered.lower(), rec["slug"]
    for rec in candidacies:
        rendered = render_candidacy(CandidacyRecord(rec["verdict"], rec["data"]))
        assert "ground truth" not in rendered.lower(), rec["verdict"]


def test_no_template_in_either_table_can_reach_for_the_phrase():
    """Covers the templates the scenario sweep does not reach: every note and
    candidacy template is rendered against a permissive stub payload, so a NEW
    template cannot smuggle the phrase back in behind an unexercised branch."""

    class _Stub(float):
        """Answers whatever a template asks of a value: number, str, mapping."""

        def __new__(cls):
            return super().__new__(cls, 1.0)

        def __getitem__(self, _key):
            return _Stub()

        def __iter__(self):
            return iter(())

        def __bool__(self):
            return False

        def __str__(self):
            return "x"

    class _AnyPayload(dict):
        def __missing__(self, _key):
            return _Stub()

    checked = 0
    for table in (_NOTE_TEMPLATES, _CANDIDACY_TEMPLATES):
        for key, template in table.items():
            try:
                rendered = template(_AnyPayload())
            except Exception:
                continue  # shape-dependent template; the sweep above covers it
            checked += 1
            assert "ground truth" not in rendered.lower(), key
    assert checked, "the stub payload rendered nothing — the guard is inert"


def test_score_override_reason_names_the_terminal_as_an_assessment():
    """The verifier floor's own reason cited "terminal ground truth" while
    striking through a judged number on the strength of another judged number."""
    reason = render_score_override_reason()
    assert reason.startswith("PASS refuted by the checkable terminal assessment")
    assert "ground truth" not in reason.lower()


def test_finding_and_defect_channel_words():
    det = Finding(
        kind="contract_breach", channel="deterministic", subject="run:think",
        data={"key": "file_type", "from": "docx", "to": "md"},
        provenance=RuleFingerprint(rule="contract"), certainty=1.0,
    )
    judged = Finding(
        kind="content_score", channel="judged", subject="run:think",
        data={"score": 0.2}, provenance=RuleFingerprint(rule="judge"),
        certainty=0.6,
    )
    assert "(deterministic)" in render_finding(det)
    assert "(assessment)" in render_finding(judged)
    assert "ground truth" not in render_finding(det).lower()


# --- 4. caveats are chips, never clippable mid-sentence prose ------------


def test_defect_caveats_render_as_trailing_chips():
    d = Defect(
        kind="content", channel="judged", origin=Localized(run_id="think"),
        base_assumed=True, observability_boundary=True,
        unverified_in_channel="contract", recovered=True,
    )
    rendered = render_defect(d)
    assert rendered.endswith(
        "[baseline assumed · observability boundary · unverified in contract "
        "· recovered downstream]"
    )
    # The head states the origin resolution; the chips are separable from it.
    assert rendered.startswith("content defect (assessment) — localized at think")


def test_origin_sum_type_renders_every_member():
    for origin, expected in (
        (Localized(run_id="act"), "localized at act"),
        (Unlocalized(reason="no candidate"), "observed but not localized (no candidate)"),
        (Design(reason="no verifier owns form"), "a design-level gap (no verifier owns form)"),
    ):
        d = Defect(kind="content", channel="judged", origin=origin)
        assert expected in render_defect(d)


def test_origin_reasons_are_codes_resolved_by_one_table():
    """`Origin.reason` is an identifier, so "why localization failed" is
    queryable and comparable across runs instead of a sentence to diff."""
    assert origin_reason_phrase(REASON_NO_CONTENT_CANDIDATE).startswith(
        "terminal content is bad but no node qualifies"
    )
    assert origin_reason_phrase(REASON_ORCHESTRATION_LAYER).startswith(
        "no node individually failed"
    )
    assert origin_reason_phrase(REASON_NO_FORM_VERIFIER).startswith(
        "no verifier charter in this graph"
    )
    assert origin_reason_phrase(REASON_INPUT_ALREADY_FLAWED) == (
        "input entered the graph already flawed"
    )
    # An unknown code renders verbatim: pre-collapse defects stored PROSE in
    # this field, and those reports must keep reading exactly as they did.
    assert origin_reason_phrase("some older sentence") == "some older sentence"


def test_emitted_origin_reasons_are_all_known_codes(mk):
    """No emitter may put a sentence in `reason` again — every origin the engine
    produces must carry a code the table resolves."""
    known = {
        REASON_NO_CONTENT_CANDIDATE, REASON_ORCHESTRATION_LAYER,
        REASON_NO_FORM_VERIFIER, REASON_INPUT_ALREADY_FLAWED,
    }
    seen = set()
    for inp in _scenarios(mk):
        for d in find_blame(inp).evidence.defects:
            reason = d["origin"].get("reason")
            if reason:
                seen.add(reason)
    assert seen, "the scenario set produced no non-localized origin"
    assert seen <= known, f"free-text origin reason(s): {sorted(seen - known)}"


def test_provenance_labels_resolve_every_code_the_engine_emits(mk):
    """`provenance.detail` is a source identifier, not a sentence — and the
    serialized `label` is its one rendering, carried on the wire so the UI needs
    no copy of the table (and so `DefectCard` has a field that actually exists)."""
    for inp in _scenarios(mk):
        for f in find_blame(inp).evidence.findings:
            prov = f["provenance"]
            detail = prov.get("detail") or ""
            if detail:
                assert detail in PROVENANCE_LABELS, detail
            assert prov["label"]
            # The label is derived, never read back.
            assert "label" not in vars(deserialize_finding(f).provenance)


# --- 5. representative sentences (the wording locks live HERE only) ------


def test_cut_point_loop_variant_names_the_cycle_not_a_retry_loop():
    rendered = render_note(
        NoteRecord(
            "cut_point",
            {"variant": "loop", "run_id": "w2", "score": 0.1, "drop": 0.8,
             "members": 4, "exit_run_id": "orch", "drilled": True},
        )
    )
    assert "4-member cycle" in rendered
    assert "retry loop" not in rendered
    assert "the loop's exit 'orch' only carried it downstream" in rendered

    # Not drilled: the culprit IS the exit, so the "only carried it" clause
    # would be a self-contradiction and must not render.
    same_node = render_note(
        NoteRecord(
            "cut_point",
            {"variant": "loop", "run_id": "orch", "score": 0.1, "drop": 0.8,
             "members": 4, "exit_run_id": "orch", "drilled": False},
        )
    )
    assert "only carried it" not in same_node
    assert "(which is also the cycle's exit node)" in same_node


def test_degraded_recovered_leads_with_the_channel_that_localized_it():
    """A deterministic origin is NOT localized by a sub-threshold score — its
    judged score is untouched and often healthy, so "scored 0.89 (below
    threshold 0.50)" would be a plain falsehood (§11 row 5)."""
    data = {
        "agent": "think", "score": 0.89, "threshold": 0.5,
        "violations": [{"key": "file_type", "from": "docx", "to": "md"}],
        "terminal_reasoning": "content complete",
    }
    det = render_note(NoteRecord("degraded_recovered", {**data, "via": "deterministic"}))
    assert "passed on content (judged 0.89) but a deterministic check failed here" in det
    assert "below threshold" not in det

    content = render_note(NoteRecord("degraded_recovered", {**data, "via": "content"}))
    assert "scored 0.89 (below threshold 0.50)" in content
    # Either way the content-only recovery caveat rides the same violations.
    for rendered in (det, content):
        assert "recovery is proven for CONTENT only" in rendered


def test_verification_gap_quotes_the_terminal_only_when_it_is_one():
    """A verdict_scored_incorrect gap rests on the role-aware judge, NOT on the
    terminal. Asserting a bad terminal here — worse, while quoting an OK
    verdict's reasoning — is the exact dishonesty the split killed."""
    base = {
        "gaps": [{"agent": "review", "score": 0.27,
                  "basis": "verdict_scored_incorrect", "issued_fail": True}],
        "threshold": 0.5,
        "terminal_reasoning": "looks complete",
        "terminal_score": 1.0,
    }
    ok = render_note(NoteRecord("verification_gap", {**base, "terminal": "ok"}))
    assert "false alarm the ok terminal contradicts" in ok
    assert "terminal output is bad" not in ok

    none = render_note(NoteRecord("verification_gap", {**base, "terminal": None}))
    assert "Terminal evidence" not in none
    assert "The terminal verdict is ok" not in none


def test_unclassified_lists_every_precondition_that_ruled_a_verdict_out():
    rendered = render_note(
        NoteRecord(
            "unclassified",
            {"reasons": [
                {"code": "terminal_ok", "score": 1.0},
                {"code": "unhealthy_not_origin", "agents": ["b"]},
            ]},
        )
    )
    assert rendered.startswith("unclassified: no origin localised — ")
    assert "terminal verdict is ok (score=1.0)" in rendered
    assert "below-threshold node(s) ['b'] did not qualify as an origin" in rendered


def test_escalated_candidacy_leads_with_the_hard_check_never_a_score_drop():
    """The escalation branch fires only for a shipped CONTRACT breach, so the
    judged score is untouched and typically ABOVE threshold — a "degraded here"
    framing would be plainly false (§11 rows 6/7)."""
    rendered = render_candidacy(
        CandidacyRecord(
            "origin_escalated",
            {"score": 0.89,
             "shipped": [{"key": "file_type", "from": "docx", "to": "md",
                          "basis": "contract param match"}]},
        )
    )
    assert rendered.startswith(
        "origin (escalated) — a deterministic contract check FAILED here "
        "(judged 0.89, untouched)"
    )
    assert "degraded here" not in rendered
    assert "near-miss" in rendered and "no longer a near-miss" in rendered


def test_escalated_candidacy_omits_the_score_clause_when_unscored():
    rendered = render_candidacy(
        CandidacyRecord(
            "origin_escalated",
            {"score": None,
             "shipped": [{"key": "file_type", "from": "docx", "to": "md",
                          "basis": "artifact path 'x.md' ends '.md'"}]},
        )
    )
    assert "judged" not in rendered
    assert rendered.startswith(
        "origin (escalated) — a deterministic contract check FAILED here and "
        "the breach was VERIFIED"
    )


@pytest.mark.parametrize(
    "why,expected",
    [
        ("no_predecessor", "no scored predecessor to measure a break against"),
        ("predecessor_also_low", "so quality was not observed to break HERE"),
        ("drop_under_gap", "stayed under the gap threshold 0.25"),
    ],
)
def test_below_not_origin_states_the_actual_reason(why, expected):
    rendered = render_candidacy(
        CandidacyRecord(
            "below_not_origin",
            {"score": 0.3, "threshold": 0.5, "base": 0.4,
             "gap_threshold": 0.25, "why": why},
        )
    )
    assert expected in rendered
    # No origin was localized, so nothing upstream may be implied.
    assert "there is nothing upstream this was shadowed by" in rendered
