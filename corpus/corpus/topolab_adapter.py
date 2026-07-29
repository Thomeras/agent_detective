"""Agent-level spans for agent_topo_db, added from OUTSIDE its source tree.

agent_topo_db ships 22 topologies and deliberately no telemetry. That makes it
the right foreign corpus: nothing in it was written with this analysis in mind,
so anything the mapper gets wrong here is a real integration bug rather than an
artifact of our own exporter agreeing with itself.

Two facts do the work:

- ``topolab.Agent.run(task, context)`` takes ``context`` as a mapping whose KEYS
  are the names of the agents whose output is being passed in. That is the
  execution graph, already written down by the topology author for entirely
  unrelated reasons. No topology needs editing to be traceable.
- Auto-instrumentation alone is not enough. ``openinference-instrumentation-openai``
  emits ``LLM`` spans, and the mapper opens a run only on
  ``openinference.span.kind=AGENT``: a purely auto-instrumented run of these
  topologies reconstructs to a graph with zero nodes. The agent layer has to be
  stated by an adapter, which is exactly the situation of every real framework
  integration.

Deliberately does NOT set ``gen_ai.request.model`` on the agent span. Standard
GenAI semconv puts it on the LLM child, and the mapper claims to fall back to
the first member span carrying it — a claim this corpus is here to check rather
than to sidestep.
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from opentelemetry import trace
from opentelemetry.trace import SpanKind

# Attempts of one agent need distinct names or reconstruction draws no edge
# between them (spans sharing gen_ai.agent.name get none). The ordinal rides
# along in agent_detective.attempt/.attempt_of so the loop check can count
# rounds rather than cycle size.
ATTEMPT_SEPARATOR = "#"

_MAX_PAYLOAD = 64_000

_usage = threading.local()


def install_usage_capture() -> bool:
    """Record what each completion actually spent, straight off the provider.

    Not a nicety. An empty output with tokens spent is an agent that returned
    nothing; an empty output with no usage recorded is an exporter that might
    have dropped it — and the analysis is right to refuse to tell them apart.
    Without this the corpus can only ever produce the weaker diagnosis, and the
    first entry recorded here hit exactly that case.

    Returns False when there is nothing to hook (dry-run has no client).
    """
    try:
        from topolab.llm import _client

        completions = _client().chat.completions
        original = completions.create

        def create(*args, **kwargs):
            body = dict(kwargs.get("extra_body") or {})
            body.setdefault("usage", {"include": True})  # OpenRouter returns real cost
            kwargs["extra_body"] = body
            response = original(*args, **kwargs)
            _usage.last = getattr(response, "usage", None)
            return response

        completions.create = create
        return True
    except Exception:  # noqa: BLE001 - telemetry must never take a run down
        return False


def _record_usage(span, model: str) -> None:
    usage = getattr(_usage, "last", None)
    _usage.last = None
    span.set_attribute("gen_ai.request.model", model)
    if usage is None:
        return
    cost = getattr(usage, "cost", None)
    if cost is None:
        cost = (getattr(usage, "model_extra", None) or {}).get("cost")
    # Report what was MEASURED; an omitted field stays honestly unknown.
    if cost is not None:
        span.set_attribute("gen_ai.usage.cost", float(cost))
    if getattr(usage, "prompt_tokens", None) is not None:
        span.set_attribute("gen_ai.usage.input_tokens", int(usage.prompt_tokens))
    if getattr(usage, "completion_tokens", None) is not None:
        span.set_attribute("gen_ai.usage.output_tokens", int(usage.completion_tokens))


def _encode(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= _MAX_PAYLOAD:
        return text
    return text[:_MAX_PAYLOAD] + f"\n…[truncated {len(text) - _MAX_PAYLOAD} chars]"


def _flatten_context(context: Mapping[str, str] | None) -> str:
    """The input payload as the agent actually received it."""
    if not context:
        return ""
    return "\n\n".join(f"--- {name} ---\n{text}" for name, text in context.items())


class TopolabTracer:
    """Wraps ``topolab.Agent.run`` for the duration of one topology run."""

    def __init__(self, tracer, root_span, injector=None) -> None:
        self._tracer = tracer
        self._root = root_span
        self._injector = injector
        self._lock = threading.Lock()
        # agent name -> the span of its most recent call, for edge resolution
        self._latest: dict[str, Any] = {}
        self._counts: dict[str, int] = {}
        self._original = None

    # -- naming ------------------------------------------------------------- #

    def _next_name(self, agent_name: str) -> tuple[str, int]:
        """``builder`` -> ``builder``, then ``builder#2``, ``builder#3``...

        The first call keeps the bare name: numbering every node in a topology
        where nothing repeats would make every corpus entry harder to read for
        no gain, and the mapper only needs the names to be unique.
        """
        count = self._counts.get(agent_name, 0) + 1
        self._counts[agent_name] = count
        return (agent_name if count == 1 else f"{agent_name}{ATTEMPT_SEPARATOR}{count}"), count

    # -- edges -------------------------------------------------------------- #

    def _upstreams(self, context: Mapping[str, str] | None) -> list[Any]:
        """The spans named by the context keys, in the order the author wrote.

        Keys that name no agent this run has seen are literal data the topology
        passed in (``schema``, ``metriky``, the raw log). They are inputs, not
        edges, and are left alone.
        """
        if not context:
            return []
        return [self._latest[name] for name in context if name in self._latest]

    def _delegation(self, parent_span, target_name: str) -> None:
        """A TOOL span carrying the second inbound edge.

        A span has one parent, so nesting expresses a fan-OUT and never a
        fan-IN: without this, a joiner has no link to the arms it merged and
        blame stops at the join.
        """
        with self._tracer.start_as_current_span(
            f"read:{target_name}",
            context=trace.set_span_in_context(parent_span),
            kind=SpanKind.INTERNAL,
        ) as tool_span:
            tool_span.set_attribute("openinference.span.kind", "TOOL")
            tool_span.set_attribute("gen_ai.tool.name", f"read:{target_name}")
            tool_span.set_attribute("gen_ai.tool.target_agent", target_name)

    # -- the wrapper -------------------------------------------------------- #

    def _run(self, agent, task: str, context: Mapping[str, str] | None = None) -> str:
        with self._lock:
            upstreams = self._upstreams(context)
            span_name, attempt = self._next_name(agent.name)
            # Parent on the first upstream so the handoff is a real edge; with
            # no upstream the run root is the honest parent. Explicit context,
            # never ambient: these topologies run branches in threads, where
            # the ambient span belongs to whichever thread got there first.
            parent = upstreams[0] if upstreams else self._root

        span = self._tracer.start_span(
            span_name,
            context=trace.set_span_in_context(parent),
            kind=SpanKind.INTERNAL,
        )
        span.set_attribute("openinference.span.kind", "AGENT")
        span.set_attribute("gen_ai.agent.name", span_name)
        span.set_attribute("input.value", _encode(_flatten_context(context) or task))
        if attempt > 1:
            span.set_attribute("agent_detective.attempt", attempt)
            span.set_attribute("agent_detective.attempt_of", agent.name)

        try:
            with trace.use_span(span, end_on_exit=False):
                output = self._original(agent, task, context)
            # Spend is read before any injection: it is what the provider
            # actually charged for the real completion, and a fault applied
            # afterwards does not un-spend it.
            _record_usage(span, agent.model)
            if self._injector is not None:
                # The faulted value is what the topology carries onward, so it
                # is also what the span must record. Injecting after the span
                # was written would produce a trace of a run that never happened.
                output = self._injector.transform(agent.name, output)
            span.set_attribute("output.value", _encode(output))
            # Every upstream after the first is a flow the span tree cannot hold.
            for extra in upstreams[1:]:
                self._delegation(span, extra.attributes["gen_ai.agent.name"])
            with self._lock:
                self._latest[agent.name] = span
            return output
        finally:
            span.end()

    # -- lifecycle ---------------------------------------------------------- #

    def install(self) -> None:
        from topolab.llm import Agent

        if self._original is not None:
            return
        self._original = Agent.run
        tracer = self

        def run(self_agent, task: str, context: Mapping[str, str] | None = None) -> str:
            return tracer._run(self_agent, task, context)

        Agent.run = run

    def uninstall(self) -> None:
        if self._original is None:
            return
        from topolab.llm import Agent

        Agent.run = self._original
        self._original = None
