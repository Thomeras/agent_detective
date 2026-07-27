"""Node-role heuristics (blame_engine.roles).

Four decisions read these hints — which node can open a verification gap, which
run gets graded as the deliverable, which rubric the judge is given, and how the
UI groups the score map — and until this module existed they were three separate
literal tuples plus a fourth for planners. The cases below are the measured
failures of the old ``any(hint in name)`` substring test.
"""

import pytest

from blame_engine.roles import is_planner, is_verifier


@pytest.mark.parametrize(
    "name",
    [
        "qa", "qa_gate", "eval", "eval_agent", "evaluator", "reviewer",
        "compliance_check", "verifier", "validator", "critic", "auditor",
        "fact_checker",
        # camelCase and title-case forms of the same roles
        "qaGate", "QA Engineer", "Quality Assurance Engineer",
        # were MISSED entirely by the substring list
        "judge", "tester", "inspector", "grader", "linter", "guardrail",
        "moderator", "approver", "assessor", "referee",
        # scoring / screening lanes an LLM-as-judge node is commonly named after
        "scorer", "rater", "ranker", "examiner", "screener", "watchdog",
        "sentinel", "arbitrator", "jury", "qc_agent",
        # multi-word role strings frameworks emit verbatim (CrewAI `role:`)
        "Software Quality Control Engineer",
        "Chief Software Quality Control Engineer",
        "Red Team Analyst", "Peer Review Agent", "Code Review Bot",
        "Acceptance Test Runner",
    ],
)
def test_verifier_names(name) -> None:
    assert is_verifier(name)


def test_crewai_harness_roles_classify_as_shipped() -> None:
    """The roles the CrewAI corpus cell actually emits: the engineer produces,
    both QC roles verify."""
    assert not is_verifier("Senior Software Engineer")
    assert is_verifier("Software Quality Control Engineer")
    assert is_verifier("Chief Software Quality Control Engineer")


@pytest.mark.parametrize(
    "name",
    [
        # 'gate' / 'check' inside ordinary words: these were FALSE verifiers, and
        # a producer mistaken for a verifier is skipped by content checks, walked
        # past when picking the deliverable, and judged by the wrong rubric.
        "delegate", "delegate_agent", "investigate_agent", "checkout_agent",
        # ordinary producers
        "scraper", "translator", "publisher", "writer", "researcher",
        # an orchestrator is a planner, never a verifier
        "supervisor", "orchestrator",
        None, "",
    ],
)
def test_non_verifier_names(name) -> None:
    assert not is_verifier(name)


@pytest.mark.parametrize(
    "name",
    ["think", "planner", "orchestrator", "router", "coordinator", "supervisor",
     "dispatcher", "task_planner",
     # the framework words for "hands work out rather than doing it"
     "manager", "project_manager", "delegator", "delegate", "scheduler",
     "triage_agent", "director", "controller"],
)
def test_planner_names(name) -> None:
    assert is_planner(name)


@pytest.mark.parametrize(
    "name",
    [
        # 'plan' inside an ordinary word: a renderer judged as a PLANNER is told
        # its correct output is an outline, not the artifact it actually ships.
        "floorplan_renderer",
        "writer", "scraper", "qa", None, "",
        # ambiguous English words kept OUT on purpose
        "lead_generator", "solution_architect", "ui_designer", "chief_engineer",
    ],
)
def test_non_planner_names(name) -> None:
    assert not is_planner(name)


def test_verifier_wins_over_planner_when_a_name_carries_both() -> None:
    """The verifier word is the JOB; the planner word only says what is checked.
    ``node_role`` tests verifier first for exactly this reason."""
    for name in ("quality_controller", "plan_reviewer", "review_coordinator"):
        assert is_verifier(name), name
