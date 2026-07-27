"""Shared fixtures: OTLP payload construction and a deterministic judge stub.

Everything here is hermetic — no network, no model, no clock. The judge stub
answers from a table keyed on agent name so a test can state "the scraper
scores 0.2" and assert on the verdict that follows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from detective_cli.judge import JudgeChoice

_AGENT_RE = re.compile(r"agent named `([^`]+)`")


class StubJudge:
    """A ``JudgeClient`` answering from canned tables, keyed by prompt shape."""

    def __init__(
        self,
        *,
        node_scores: dict[str, dict[str, Any]] | None = None,
        terminal: dict[str, Any] | None = None,
    ) -> None:
        self.node_scores = node_scores or {}
        self.terminal = terminal or {
            "verdict": "ok",
            "score": 0.95,
            "reasoning": "the deliverable meets the request",
        }
        self.prompts: list[str] = []

    async def complete_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        self.prompts.append(prompt)
        if "final quality gate" in prompt:
            return dict(self.terminal)
        if "auditing one step" in prompt:
            return {"claims": []}
        match = _AGENT_RE.search(prompt)
        agent = match.group(1) if match else "unknown"
        return dict(
            self.node_scores.get(
                agent, {"task_score": 0.9, "input_flawed": False, "reasoning": "fine"}
            )
        )

    async def close(self) -> None:
        pass


def judging(judge: StubJudge) -> JudgeChoice:
    """Wrap a stub as the ``JudgeChoice`` the analysis pipeline expects."""
    return JudgeChoice(client=judge, enabled=True, description="stub")


def span(
    *,
    name: str,
    span_id: str,
    trace_id: str = "1" * 32,
    parent_span_id: str = "",
    agent_name: str | None = None,
    output: str = "some output text",
    input_text: str = "do the thing",
    start_ns: int = 1_000_000_000,
    end_ns: int = 2_000_000_000,
    extra_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One OTLP/HTTP JSON span with OpenInference agent conventions."""
    attributes: list[dict[str, Any]] = [
        {"key": "openinference.span.kind", "value": {"stringValue": "AGENT"}},
        {"key": "input.value", "value": {"stringValue": input_text}},
        {"key": "output.value", "value": {"stringValue": output}},
    ]
    if agent_name is not None:
        attributes.append(
            {"key": "gen_ai.agent.name", "value": {"stringValue": agent_name}}
        )
    for key, value in (extra_attributes or {}).items():
        attributes.append({"key": key, "value": {"stringValue": str(value)}})
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes,
        "status": {"code": 1},
    }


def export(spans: list[dict[str, Any]], *, service_name: str = "test-pipeline") -> dict[str, Any]:
    """Wrap spans in an ExportTraceServiceRequest with a resource service.name."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}}
                    ]
                },
                "scopeSpans": [{"scope": {"name": "test"}, "spans": spans}],
            }
        ]
    }


def linear_pipeline(
    outputs: dict[str, str] | None = None, *, service_name: str = "test-pipeline"
) -> dict[str, Any]:
    """A three-node chain: planner -> writer -> reviewer, each a child of the last.

    Parent/child nesting is what the mapper derives edges from, so this is the
    smallest input that produces a graph with real propagation.
    """
    outputs = outputs or {}
    names = ["planner", "writer", "reviewer"]
    spans: list[dict[str, Any]] = []
    for index, agent in enumerate(names):
        spans.append(
            span(
                name=f"{agent}.run",
                span_id=f"{index + 1:016x}",
                parent_span_id="" if index == 0 else f"{index:016x}",
                agent_name=agent,
                output=outputs.get(agent, f"{agent} produced a complete result"),
                start_ns=1_000_000_000 + index * 1_000_000_000,
                end_ns=2_000_000_000 + index * 1_000_000_000,
            )
        )
    return export(spans, service_name=service_name)


@pytest.fixture
def trace_file(tmp_path: Path):
    """Write an export payload to a file and hand back the path."""

    def _write(payload: Any, name: str = "trace.json") -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def demo_traces() -> dict[str, Path]:
    """The repository's recorded demo traces, when running from a checkout.

    Skipped rather than failed when absent: the wheel ships without testdata,
    and a test that cannot see its fixture has not found a defect.
    """
    root = Path(__file__).resolve().parents[3] / "testdata"
    happy = root / "demo_pipeline_happy.json"
    faulted = root / "demo_pipeline_faulted.json"
    if not (happy.exists() and faulted.exists()):
        pytest.skip("repository testdata/ not available")
    return {"happy": happy, "faulted": faulted}
