"""Shared BlameInput factory for behavior tests."""

from collections.abc import Mapping, Sequence

import pytest

from blame_engine import BlameConfig, BlameInput, NodeScore


def _wrap_score(run_id: str, value: float | None | NodeScore) -> NodeScore:
    if isinstance(value, NodeScore):
        return value
    return NodeScore(
        run_id=run_id,
        score=value,
        components={},
        input_flawed=None,
        unscored_reason="payload_missing" if value is None else None,
        judge_note=None,
    )


def make_input(
    *,
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]] = (),
    scores: Mapping[str, float | None | NodeScore] | None = None,
    costs: Mapping[str, float] | None = None,
    end_times: Mapping[str, float] | None = None,
    agent_names: Mapping[str, str] | None = None,
    error_span_ids: Mapping[str, list[str]] | None = None,
    terminal_verdict=None,
    loop_baselines=None,
    config: BlameConfig | None = None,
    attempts: Mapping[str, tuple[str, int]] | None = None,
) -> BlameInput:
    """Build a BlameInput with deterministic defaults.

    scores maps run_id -> float score, None (unknown), or a full NodeScore.
    Default end_times follow node order (0.0, 1.0, ...), costs default to 1.0,
    agent_names default to the run_id itself. ``attempts`` maps run_id ->
    (agent the attempt belongs to, which attempt it was), the loop identity an
    instrumented retry records.
    """
    node_list = list(nodes)
    raw_scores = scores or {}
    return BlameInput(
        nodes=node_list,
        edges=list(edges),
        scores={n: _wrap_score(n, raw_scores.get(n)) for n in node_list},
        node_costs={n: (costs or {}).get(n, 1.0) for n in node_list},
        node_end_times={
            n: (end_times or {}).get(n, float(i)) for i, n in enumerate(node_list)
        },
        agent_names={n: (agent_names or {}).get(n, n) for n in node_list},
        error_span_ids=dict(error_span_ids or {}),
        terminal_verdict=terminal_verdict,
        loop_baselines=dict(loop_baselines or {}),
        config=config or BlameConfig(),
        node_attempts={n: a[1] for n, a in (attempts or {}).items()},
        node_attempt_of={n: a[0] for n, a in (attempts or {}).items()},
    )


@pytest.fixture
def mk():
    return make_input


# --- Typed rationale accessors (verdict refactor §2.4) -------------------
#
# Notes and candidacy are RENDER artifacts. Asserting on their sentences made
# every rewording a breaking change and let a template drift away from the data
# it claims to describe without a single test noticing (§11 row 19). These
# helpers read the typed records instead — the slug, the candidacy verdict and
# their payloads — which is all a behaviour test is entitled to know. The prose
# itself is covered exactly once, in test_narrative.py.


def note_slugs(report) -> list[str]:
    """Slugs of every note the report emitted, in order."""
    return [n["slug"] for n in report.evidence.note_records]


def notes_of(report, slug: str, **match):
    """Every note record with ``slug`` whose data matches the given fields."""
    return [
        n["data"]
        for n in report.evidence.note_records
        if n["slug"] == slug
        and all(n["data"].get(k) == v for k, v in match.items())
    ]


def note_of(report, slug: str, **match):
    """The single note record with ``slug``, or None when it did not fire."""
    found = notes_of(report, slug, **match)
    assert len(found) <= 1, f"expected at most one {slug!r} note, got {len(found)}"
    return found[0] if found else None


def verdict_of(report, run_id: str) -> str:
    """The candidacy VERDICT code for one node (e.g. 'origin_deterministic')."""
    return report.evidence.candidacy_records[run_id]["verdict"]


def candidacy_of(report, run_id: str) -> dict:
    """The candidacy payload for one node (the numbers the decision rested on)."""
    return report.evidence.candidacy_records[run_id]["data"]
