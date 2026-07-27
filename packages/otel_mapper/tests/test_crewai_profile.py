"""CrewAI framework profile — measured on the foreign corpus cell (night_run
2026-07-24): vanilla OpenInference CrewAI emits AGENT task spans named
``"<Role>.<task>._execute_core"`` with no gen_ai.agent.name, wraps payloads in
task-metadata JSON, and parents every task on ``Crew.kickoff`` (no data-flow
spans). Span shapes below mirror the captured trace. The profile must recover
names, unwrap payloads, and derive the Process.sequential chain — and must
NEVER touch a generic trace (gating tests at the bottom)."""

import json

from otel_mapper import EdgeType, map_spans


def _span(span_id, name, *, parent=None, start, attrs=None):
    return {
        "trace_id": "tc",
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "start_time": f"2026-07-24T11:58:0{start}.000000+00:00",
        "end_time": f"2026-07-24T11:58:0{start + 1}.000000+00:00",
        "attributes": attrs or {},
    }


def _task_span(span_id, role, task, *, start, output_raw):
    return _span(
        span_id,
        f"{role}.{task}._execute_core",
        parent="k1",
        start=start,
        attrs={
            "openinference.span.kind": "AGENT",
            "input.value": json.dumps(
                {
                    "agent": {"role": role + "\n", "goal": "g", "backstory": "b"},
                    "description": f"You will create a game: instructions for {task}",
                }
            ),
            "output.value": json.dumps(
                {"description": f"{task} done", "raw": output_raw}
            ),
        },
    )


def _crew_trace():
    return [
        _span("k1", "GameBuilderCrew.kickoff", start=0),
        _task_span("s1", "Senior Software Engineer", "code_task", start=1,
                   output_raw="import pygame  # code"),
        _task_span("s2", "Software Quality Control Engineer", "review_task",
                   start=3, output_raw="import pygame  # reviewed"),
        _task_span("s3", "Chief Software Quality Control Engineer",
                   "evaluate_task", start=5, output_raw="import pygame  # final"),
    ]


def test_crewai_agent_names_recovered_from_span_names() -> None:
    runs = sorted(map_spans(_crew_trace()).runs, key=lambda r: r.start_time)
    assert [r.agent_name for r in runs] == [
        "Senior Software Engineer",
        "Software Quality Control Engineer",
        "Chief Software Quality Control Engineer",
    ]


def test_crewai_payloads_unwrapped_to_description_and_raw() -> None:
    runs = sorted(map_spans(_crew_trace()).runs, key=lambda r: r.start_time)
    assert runs[0].input == "You will create a game: instructions for code_task"
    assert runs[0].output == "import pygame  # code"
    assert runs[2].output == "import pygame  # final"


def test_crewai_sequential_chain_derived_under_one_kickoff() -> None:
    result = map_spans(_crew_trace())
    chain = [
        (e.from_run_key, e.to_run_key)
        for e in result.edges
        if e.type == EdgeType.A2A_MESSAGE
    ]
    assert chain == [("tc:s1", "tc:s2"), ("tc:s2", "tc:s3")]
    assert all(
        "crewai_sequential" in e.detection_method
        for e in result.edges
        if e.type == EdgeType.A2A_MESSAGE
    )


def test_crewai_chain_survives_batch_split_without_the_kickoff_span() -> None:
    """The kickoff span is the trace ROOT: it ends last and ships in a LATER
    OTLP batch — per-batch mapping never sees it next to its children. The
    chain must derive from the shared parent id + name signature alone."""
    spans = [s for s in _crew_trace() if s["span_id"] != "k1"]
    result = map_spans(spans)
    chain = [
        (e.from_run_key, e.to_run_key)
        for e in result.edges
        if e.type == EdgeType.A2A_MESSAGE
    ]
    assert chain == [("tc:s1", "tc:s2"), ("tc:s2", "tc:s3")]


def test_crewai_explicit_gen_ai_name_still_wins() -> None:
    spans = _crew_trace()
    spans[1]["attributes"]["gen_ai.agent.name"] = "coder"
    runs = sorted(map_spans(spans).runs, key=lambda r: r.start_time)
    assert runs[0].agent_name == "coder"


def test_generic_traces_are_never_touched_by_the_profile() -> None:
    """No _execute_core signature → no invented names, no invented edges, raw
    payloads untouched (JSON object stays serialized as-is)."""
    payload = json.dumps({"description": "not crewai", "raw": "nope"})
    spans = [
        _span("k1", "orchestrator.kickoff", start=0),
        _span("a1", "worker_a", parent="k1", start=1,
              attrs={"openinference.span.kind": "AGENT", "input.value": payload}),
        _span("a2", "worker_b", parent="k1", start=3,
              attrs={"openinference.span.kind": "AGENT", "input.value": payload}),
    ]
    result = map_spans(spans)
    assert [r.agent_name for r in result.runs] == [None, None]
    assert all(r.input == payload for r in result.runs)
    assert [e for e in result.edges if e.type == EdgeType.A2A_MESSAGE] == []
