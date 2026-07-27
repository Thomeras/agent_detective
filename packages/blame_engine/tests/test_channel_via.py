"""Spec (RED): the quality channel and the localisation channel must be separated.

Today the worker floors a deterministically-refuted producer to a sentinel
(`_CONTRACT_VIOLATION_SCORE = 0.15`) so that `degraded = score < threshold`
(cutpoint.py) drags the node into candidacy. That multiplexes TWO different facts
onto one scalar:

  - "how good is the output?"  (the judge's measured quality)
  - "did a hard check fail here?" (localisation)

Consequences proven against the live code earlier in this investigation:
  * the judged score (0.89) survives only as struck-through decoration — the
    engine's candidacy/shadowing/confidence all read the sentinel, so the most
    valuable datum (a 74-point judge miss caught by a hard check) drives nothing;
  * `degraded = s < cfg.threshold` (cutpoint.py:190) is the ONLY path into
    candidacy — a deterministic breach has no independent channel, so naively
    "un-flooring" the score to 0.89 would switch blame off entirely;
  * shadowing runs over ONE flat origin list (cutpoint.py:275-282), so a content
    origin upstream buries a point-attributable contract origin downstream.

The fix is a first-class provenance on the candidate — `Candidate.via ∈
{"content", "deterministic", "both"}` — with per-channel candidacy AND per-channel
shadowing. These two tests encode that contract. They are RED until the
decoupling lands (no `via` field yet; deterministic candidacy not wired; content
shadowing still crosses channels). Build the refactor until they go green; do not
weaken the asserts to match today's behaviour.

Note on the seam: these graphs are built through `conftest.make_input` — the same
judge-free, DB-free entry point that fixture replay will later use. No new
constructor was needed; the seam already existed.
"""

from blame_engine import NodeScore, select_candidates


def _score(run_id: str, value: float, *, contract=()) -> NodeScore:
    """A judged NodeScore carrying its REAL (un-floored) score plus an optional
    deterministic contract violation as a SEPARATE evidence stream."""
    return NodeScore(
        run_id=run_id,
        score=value,
        components={"judge": value},
        input_flawed=None,
        unscored_reason=None,
        judge_note=None,
        contract_violations=tuple(contract),
    )


def test_deterministic_candidacy(mk):
    """A node the judge rated healthy (0.89) but which deterministically breached
    a carried contract IS a culprit — via the deterministic channel — and its
    judged score flows through untouched.

    This locks the sharpest structural finding: 0.89 >= threshold, so ONLY the
    contract breach can localise blame here. If the engine still keyed candidacy
    off the score alone, this node would be silently cleared."""
    inp = mk(
        nodes=["think", "render"],
        edges=[("think", "render")],
        scores={
            "think": _score("think", 0.89, contract=[("file_type", "docx", "md")]),
            "render": 1.0,
        },
    )

    cands = {c.run_id: c for c in select_candidates(inp)}

    # RED today: think(0.89) is >= threshold, so it is not a candidate at all.
    assert "think" in cands, (
        "a deterministic contract breach must make the node a candidate even when "
        "the judge scored it healthy — otherwise 'un-flooring' the score turns "
        "blame off, which is exactly the bug this decoupling fixes"
    )
    assert cands["think"].via == "deterministic"
    # The judged score is never rewritten to a sentinel; the refutation rides
    # alongside as evidence, not as a replacement number.
    assert inp.scores["think"].score == 0.89
    assert cands["think"].score == 0.89


def test_via_both_single_origin_two_defects(mk):
    """R1: a node that is BOTH content-degraded (score < threshold) AND
    deterministically breached is ONE origin carrying TWO defects — `via="both"`.

    Not two origins, and not dropped from either lattice: it must participate in
    the content lattice (so it can shadow / be shadowed on content) AND in the
    deterministic lattice (so its point-attributable breach is never buried).
    Without this test the implementation decides `both` silently — collapsing it to
    one channel loses a defect, splitting it to two origins double-counts."""
    inp = mk(
        nodes=["think", "render"],
        edges=[("think", "render")],
        scores={
            "think": _score("think", 0.20, contract=[("file_type", "docx", "md")]),
            "render": 1.0,
        },
    )

    cands = [c for c in select_candidates(inp) if c.run_id == "think"]

    # RED today: `via` does not exist. One node stays ONE candidate.
    assert len(cands) == 1, "one node is one origin even with two defect channels"
    assert cands[0].via == "both"


def test_deterministic_chain_second_rewrite_is_secondary_actor(mk):
    """R2: two rewrites of the SAME chain are not two origins. A (docx->md) is the
    fresh origin (its input arrived intact); B (md->txt) merely inherited an
    already-nonconformant input, so it is a secondary actor, NOT an origin.

    Both are judge-healthy (0.9), so neither is a content candidate — this isolates
    the deterministic-channel basis distinction from any score effect."""
    inp = mk(
        nodes=["A", "B", "sink"],
        edges=[("A", "B"), ("B", "sink")],
        scores={
            "A": _score("A", 0.9, contract=[("file_type", "docx", "md")]),
            "B": _score("B", 0.9, contract=[("file_type", "md", "txt")]),
            "sink": 1.0,
        },
    )

    cands = {c.run_id: c for c in select_candidates(inp)}

    assert "A" in cands and cands["A"].via == "deterministic"
    assert "B" not in cands, (
        "B's contract rewrite inherited an already-nonconformant input (md, "
        "produced by A) — a secondary actor on the same chain, not a fresh origin"
    )


def test_a_join_echoing_an_ancestors_rewritten_value_is_a_secondary_actor(mk):
    """R2 on a FAN-IN — the case the chain test above cannot reach.

    ``join`` merges two branches and reports the file type it actually produced:
    md, the value ``a`` invented. Its OWN input/output diff is indistinguishable
    from a fresh rewrite — it was handed the task's docx contract, so the input
    arrived "intact" as far as the node can tell — and the input-side basis check
    therefore looked for an ancestor rewriting TO docx, found none, and declared
    the joiner a fresh origin. The result was two deterministic culprits and a
    `multi_culprit` verdict for ONE fault: the merger blamed for the value it
    merged. Only the output side settles it — md was already in circulation from
    an ancestor.

    (On a real trace the joiner's input is usually MULTI-valued — one value per
    incoming branch — which ``worker.scoring`` now reports as ambiguous rather
    than picking one. This test covers the case that survives that fix: an
    unambiguous input contract plus an echoed output.)
    """
    inp = mk(
        nodes=["root", "a", "b", "join"],
        edges=[("root", "a"), ("root", "b"), ("a", "join"), ("b", "join")],
        scores={
            "root": 0.9,
            "a": _score("a", 0.9, contract=[("file_type", "docx", "md")]),
            "b": _score("b", 0.9),
            "join": _score("join", 0.9, contract=[("file_type", "docx", "md")]),
        },
    )

    cands = {c.run_id: c for c in select_candidates(inp)}

    assert set(cands) == {"a"}, (
        "the joiner echoed a value an ancestor put into circulation on the same "
        "key — a carrier, not a second origin"
    )
    assert cands["a"].via == "deterministic"


def test_a_descendant_rewriting_to_the_same_value_is_suppressed_too(mk):
    """RECORDED COST of the output-side basis rule, not an endorsement.

    ``b`` rewrote its own (differently-arriving) input to md, so on its own
    evidence it is a fresh rewrite; it is suppressed anyway because md already
    existed upstream. Nothing in the graph distinguishes "echoed the poisoned
    value" from "independently produced the same value" without payload-level
    dataflow, and of the two possible errors, blaming a carrier is the one that
    misdirects the reader — the first origin is still reported and the chain from
    it to here is visible. Pinned so the trade-off cannot change silently."""
    inp = mk(
        nodes=["a", "b", "sink"],
        edges=[("a", "b"), ("b", "sink")],
        scores={
            "a": _score("a", 0.9, contract=[("file_type", "docx", "md")]),
            "b": _score("b", 0.9, contract=[("file_type", "rtf", "md")]),
            "sink": 1.0,
        },
    )

    cands = {c.run_id: c for c in select_candidates(inp)}

    assert set(cands) == {"a"}


def test_deterministic_chain_independent_keys_are_both_origins(mk):
    """Guard for the R2 basis: two rewrites of DIFFERENT keys are independent
    faults — both origins, no chain inheritance between them."""
    inp = mk(
        nodes=["A", "B", "sink"],
        edges=[("A", "B"), ("B", "sink")],
        scores={
            "A": _score("A", 0.9, contract=[("file_type", "docx", "md")]),
            "B": _score("B", 0.9, contract=[("lang", "cs", "en")]),
            "sink": 1.0,
        },
    )

    cands = {c.run_id: c for c in select_candidates(inp)}

    assert cands["A"].via == "deterministic"
    assert cands["B"].via == "deterministic", (
        "B rewrote a DIFFERENT key (lang), unrelated to A's file_type chain — an "
        "independent origin, not a secondary actor"
    )


def test_channel_shadowing(mk):
    """Content shadowing must not bury an independently-attributable contract
    origin downstream.

    A (source) has a genuine content degradation. B (downstream) inherits that
    low content AND silently rewrites a carried contract parameter. B's *content*
    problem is correctly inherited from A (shadowed in the content channel), but
    its *contract* breach is point-attributable (input intact -> output rewritten)
    regardless of upstream quality, so B must survive as a DETERMINISTIC origin.

    Today both are one flat list keyed on score: B(0.20) is shadowed by its
    ancestor A(0.20) and disappears, taking the contract breach with it."""
    inp = mk(
        nodes=["A", "B", "sink"],
        edges=[("A", "B"), ("B", "sink")],
        scores={
            "A": _score("A", 0.20),
            "B": _score("B", 0.20, contract=[("file_type", "docx", "md")]),
            "sink": 1.0,
        },
    )

    cands = {c.run_id: c for c in select_candidates(inp)}

    # RED today: content shadowing drops B (ancestor A is also an origin), so the
    # contract breach in B is buried under A's content degradation.
    assert set(cands) == {"A", "B"}, (
        "per-channel shadowing must keep BOTH: A as the content origin and B as "
        "the deterministic origin — content shadowing may not cross into the "
        "deterministic channel"
    )
    assert cands["A"].via == "content"
    assert cands["B"].via == "deterministic"


def test_a_fully_suppressed_deterministic_channel_still_names_the_breach(mk):
    """R2 must not empty the verdict when the primary it points at is invisible.

    R2 ("someone upstream already put this value in circulation, so you are
    propagation, not origin") is sound only while that someone is REACHABLE. It
    is not always: a node that processed flawed input is excluded as a
    propagation point, and a cycle is inspected at its exit member only. When
    every candidate is suppressed the channel went silent over recorded hard
    evidence — observed end to end as "NOT VERIFIED / Nothing could be measured",
    exit 0, on a trace carrying two detected contract breaches.

    The honest reading is to name the reachable breach and mark that an earlier
    origin may exist — which also means the 0.95 observed-origination convention
    is NOT earned here, because origination was precisely what could not be seen.
    """
    inp = mk(
        nodes=["a", "b"],
        edges=[("a", "b")],
        scores={
            # `a` rewrote markdown->html and is excluded (it processed flawed input);
            # `b` echoes the same rewrite, so R2 calls it secondary.
            "a": NodeScore(
                run_id="a", score=0.9, components={"judge": 0.9}, input_flawed=True,
                unscored_reason=None, judge_note=None,
                contract_violations=(("format", "markdown", "html"),),
            ),
            "b": _score("b", 0.9, contract=[("format", "markdown", "html")]),
        },
    )

    cands = {c.run_id: c for c in select_candidates(inp)}
    assert "b" in cands, "silence over two recorded breaches is not an honest verdict"
    assert cands["b"].via == "deterministic"
    # The marker that keeps it honest: located here, origin possibly earlier.
    assert cands["b"].unknown_upstream is True
