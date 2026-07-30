"""Span emission — the short way to make a run analysable.

Everything else in this package helps an instrumentation that already exists.
This module IS the instrumentation, for people who do not have one: a context
manager per agent step, and an OTLP/HTTP JSON export when the run ends.

Why it exists: the conventions Agent Detective needs are simple but easy to get
subtly wrong, and getting them wrong is expensive — a span without
``openinference.span.kind=AGENT`` does not become a node at all, a node whose
``output.value`` holds a status record (``{"ok": true}``) gets its *phrasing*
judged instead of its work, and a missing cost attribute is indistinguishable
from a free run. One real integration hand-wrote 420 lines of exporter and
still hit two of those. This module is that exporter, written once.

Pure stdlib, zero dependencies — deliberately. Instrumentation lives inside the
agent's own process, so it must not drag a judge, a database, or the
OpenTelemetry SDK in with it. The payload is a plain dict; `json` can build it.

Four shapes, because topology changes the analysis:

    with run("intel", task="pre-call dossier for Alza.cz") as r:
        with r.step("resolve") as s:        # PIPELINE: parent = previous step
            s.output = company
        with r.step("collect") as s:        # input defaults to resolve's output
            s.output = docs
            s.cost(usd=0.004, tokens_in=1200, tokens_out=340, model="gpt-4o")

    with run("orchestrator", task=brief) as r:
        with r.span("planner") as p:        # TREE: parent = enclosing span
            p.output = plan
            with r.span("writer") as w:     # child of `planner`
                w.output = draft

    with run("orchestrator", task=brief) as r:
        parts = []
        for section in ("intro", "specs", "pricing"):
            with r.branch(f"write_{section}") as w:   # FAN-OUT, then
                w.output = write(section)
                parts.append(w)
        with r.join("merge", parts) as m:            # FAN-IN
            m.output = document

    with run("editor", task=brief) as r:
        with r.retry("revise_loop") as loop:         # LOOP
            while True:
                with loop.attempt("write") as a:     # agent "write#1", "write#2"
                    a.output = draft
                with loop.attempt("qa") as a:
                    a.output = verdict
                if verdict["ok"]:
                    break
            loop.output = draft

    with run("talk", task=brief) as r:
        research = r.retry("research_loop", parallel=True)   # LOOPS SIDE BY SIDE
        draft = r.retry("draft_loop", parallel=True)         # (threads, no
        ...                                                  #  edge between them)
        with r.join("merge", [research.span, draft.span]) as m:
            m.output = deck

`step` is a handoff chain (each step's input is what the previous one produced —
without that the blame engine has nothing to compare between neighbours);
`span` nests; `branch` fans out; `join` fans back IN; `retry` is a loop, and
`retry(parallel=True)` is a loop that is one ARM of a fan-out. Mixing them is
fine.

Why `join` and `retry` are constructs and not just conventions: a span carries
ONE ``parentSpanId``, so nesting can express a fan-OUT and never a fan-IN —
three workers and their joiner all end up siblings of the orchestrator and the
joiner has no incoming edge from the work it merged. And three attempts of the
same agent reconstruct as a chain, never a cycle, because same-name spans get no
edge between them at all. Both are ONLY expressible through attributes the
integrator would otherwise have to learn from the mapper's source. These two
constructs emit them (see :meth:`Span.reads_from`).

Off unless switched on: without ``AGENT_DETECTIVE_ENDPOINT`` or
``AGENT_DETECTIVE_TRACE_FILE`` (or an explicit argument) every call here is a
cheap no-op, so instrumented code can ship to production untouched.

Never raises. An observability bug must not take a run down with it — failures
disable the recorder and are logged at debug level.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from types import TracebackType
from typing import Any, Iterable, Optional

from .artifacts import artifact_meta

logger = logging.getLogger(__name__)

# Payload ceiling per attribute. Generous on purpose: Agent Detective offloads
# oversized payloads out of the span itself, and a judge that cannot see the
# work cannot score it. Truncation is ANNOUNCED (see `_encode`) — a silently
# cut payload reads as "the agent produced this much", which is a lie.
MAX_PAYLOAD_CHARS = 64_000

_ENDPOINT_ENV = "AGENT_DETECTIVE_ENDPOINT"
_TRACE_FILE_ENV = "AGENT_DETECTIVE_TRACE_FILE"
_SERVICE_ENV = "AGENT_DETECTIVE_SERVICE_NAME"

# Attempt identity: "write" run three times becomes write#1 / write#2 / write#3.
# Without distinct names the graph has no loop to find — spans that share a
# gen_ai.agent.name get NO edge between them, so a three-attempt retry
# reconstructs as three disconnected nodes. Measured, not assumed: see
# tests/test_fanin_and_retry.py::TestRetryLoopReconstructs.
ATTEMPT_SEPARATOR = "#"


def _hex(nbytes: int) -> str:
    return uuid.uuid4().hex[: nbytes * 2]


def _span_id(value: Any) -> str:
    """Normalise an externally supplied span id; anything off becomes no parent."""
    candidate = str(value or "").strip().lower()
    if len(candidate) == 16 and all(c in "0123456789abcdef" for c in candidate):
        return candidate
    return ""


def _trace_id_of(value: Any) -> str:
    """Normalise an externally supplied trace id; anything off becomes a fresh one."""
    candidate = str(value or "").strip().lower()
    if len(candidate) == 32 and all(c in "0123456789abcdef" for c in candidate):
        return candidate
    return _hex(16)


def _encode(value: Any, limit: int = MAX_PAYLOAD_CHARS) -> str:
    """Any Python value -> the string that goes into an OTLP attribute."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 - a payload must never break the run
            text = str(value)
    if len(text) <= limit:
        return text
    # Say it out loud. A judge reading a cut payload would otherwise score the
    # fragment as if it were the whole output.
    cut = len(text) - limit
    return text[:limit] + f"\n…[truncated {cut} chars by detective_sdk]"


def _attr(key: str, value: Any) -> dict:
    return {"key": key, "value": {"stringValue": str(value)}}


def _counted(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


class _Delegation:
    """A TOOL span whose only job is to carry an edge the span tree cannot.

    A span has ONE ``parentSpanId``. Nesting can therefore express a fan-OUT and
    never a fan-IN: three workers and the joiner that merged them all end up
    siblings of the orchestrator, and the joiner has no incoming edge from the
    work it merged — blame has nothing to follow back.

    ``gen_ai.tool.target_agent`` on a TOOL span is the second edge rule the
    reconstruction implements: the named agent's output flowed INTO the run that
    owns this span (edge target -> caller). It is the only way an in-process
    trace can state a second inbound flow, and it is the one attribute an
    integrator could not guess. Emitted by :meth:`Span.reads_from`, never by
    hand.

    Not an agent span: no ``openinference.span.kind=AGENT``, so it adds no node
    — only the edge.
    """

    __slots__ = ("tool", "span_id", "parent_id", "target_name", "target_span", "_created_ns")

    def __init__(
        self, tool: str, parent_id: str, target_name: str, target_span: "Span | None" = None
    ) -> None:
        self.tool = tool
        self.span_id = _hex(8)
        self.parent_id = parent_id
        # The name is what the integrator said; the span (when we have one) is
        # WHICH run they meant. Keeping both is what lets `Run.build_payload`
        # address a specific arm of a fan-out whose siblings share its name —
        # reconstruction resolves the target by name, so a name alone would land
        # the edge on whichever same-named run started first.
        self.target_name = target_name
        self.target_span = target_span
        self._created_ns = time.time_ns()

    def resolved_name(self, names: "dict[int, str]") -> Optional[str]:
        """The unique agent name this edge points at, or ``None`` for no edge.

        ``names`` maps ``id(span) -> final name`` (see ``Run._final_names``).
        A bare name the run cannot resolve to exactly one span yields ``None``:
        an edge to the wrong node is a confident lie, and silence is the honest
        alternative — the same rule :meth:`Span.reads_from` already applies when
        a source names the reader's own agent.
        """
        if self.target_span is not None:
            return names.get(id(self.target_span), self.target_name)
        return self.target_name

    def _to_otlp(self, trace_id: str, end_ns: int, target: str, start_ns: int) -> dict:
        return {
            "traceId": trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_id,
            "name": f"{self.tool}:{target}",
            "kind": 1,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(start_ns),
            "attributes": [
                _attr("openinference.span.kind", "TOOL"),
                _attr("gen_ai.tool.name", f"{self.tool}:{target}"),
                _attr("gen_ai.tool.target_agent", target),
                _attr("input.value", _encode({"target": target})),
            ],
            "status": {"code": 1},
        }


class Span:
    """One agent step. Becomes exactly one node in the execution graph.

    Set :attr:`output` to what the step actually produced — its *work*, not a
    status record. That value is what the quality judge reads; ``{"ok": true}``
    tells it nothing except how the ping was phrased.
    """

    __slots__ = (
        "name", "span_id", "parent_id", "_start_ns", "_end_ns", "_input", "output",
        "_status", "_attrs", "_artifacts", "_run", "_parallel",
    )

    def __init__(self, run: "Run", name: str, parent_id: str, *, input: Any = None) -> None:
        self._run = run
        self.name = name
        self.span_id = _hex(8)
        self.parent_id = parent_id
        self._start_ns = time.time_ns()
        self._end_ns: Optional[int] = None
        self._input = input
        self.output: Any = None
        self._status = "ok"
        self._attrs: dict[str, str] = {}
        self._artifacts: list[dict] = []
        # Set by `branch`: this span runs BESIDE its siblings, so the next
        # branch must not hang off it just because it is still open (see
        # Run._fanout_point).
        self._parallel = False

    # -- what the step reports ---------------------------------------------- #

    def cost(
        self,
        *,
        usd: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        model: str | None = None,
    ) -> "Span":
        """Attach spend. Report what you MEASURED; leave the rest out.

        Agent Detective never invents a price and ships no pricing table, so an
        omitted cost stays honestly unknown rather than becoming ``$0`` — which
        would make a metered agent look more expensive than an unmetered one.
        """
        if usd is not None:
            self._attrs["gen_ai.usage.cost"] = str(usd)
        if tokens_in is not None:
            self._attrs["gen_ai.usage.input_tokens"] = str(int(tokens_in))
        if tokens_out is not None:
            self._attrs["gen_ai.usage.output_tokens"] = str(int(tokens_out))
        if model:
            self._attrs["gen_ai.request.model"] = str(model)
        return self

    def artifact(self, path: str) -> "Span":
        """Record a file this step produced (size/hash, read from disk).

        Integrity metadata rides OUTSIDE the payload on purpose: a document's
        text can be forged by its own content, a span attribute cannot. Note
        this does NOT embed the file — if the judge should check the artifact,
        also put its text in :attr:`output`, or the terminal verdict degrades
        to ``not_checkable``.
        """
        try:
            # `path` is not part of artifact_meta's own shape, but the attribute
            # contract is a list of entries keyed by it (signals.parse_artifact_meta);
            # without it every artifact would land as "?".
            entry = {**artifact_meta(path), "path": path}
        except Exception:  # noqa: BLE001
            logger.debug("detective_sdk: artifact_meta(%s) failed", path, exc_info=True)
            return self
        self._artifacts.append(entry)
        self._attrs["agent_detective.artifact_meta"] = json.dumps(
            self._artifacts, ensure_ascii=False
        )
        return self

    def contract(self, **params: Any) -> "Span":
        """Declare parameters this step's input is contractually bound to,
        e.g. ``span.contract(file_type="pdf", lang="cs")``.

        The out-of-band lane into the deterministic contract channel: when a
        step's payloads are prose or code, the input/output JSON diff has
        nothing to parse and a silent parameter rewrite is invisible.
        Declared params stand in for the input side; the detective still has
        to SEE the changed value in the step's output to name a violation.
        Scalar values only — nested structures are ignored downstream.
        """
        if not params:
            return self
        merged = {}
        existing = self._attrs.get("agent_detective.contract_params")
        if existing:
            try:
                parsed = json.loads(existing)
                if isinstance(parsed, dict):
                    merged.update(parsed)
            except ValueError:
                logger.debug("detective_sdk: discarding malformed contract_params")
        merged.update(params)
        self._attrs["agent_detective.contract_params"] = json.dumps(
            merged, ensure_ascii=False, default=str
        )
        return self

    def version(self, *, agent: str | None = None, prompt_hash: str | None = None) -> "Span":
        """Version stamps, so scores stay comparable across prompt revisions."""
        if agent:
            self._attrs["gen_ai.agent.version"] = str(agent)
        if prompt_hash:
            self._attrs["agent_detective.prompt_hash"] = str(prompt_hash)
        return self

    def fail(self, reason: Any = None) -> "Span":
        """Mark a degraded step. A never-raise fallback still failed — say so,
        or tier1 has nothing to detect."""
        self._status = "error"
        if reason is not None and self.output is None:
            self.output = reason
        return self

    def attr(self, key: str, value: Any) -> "Span":
        """Escape hatch for any other OTLP attribute."""
        self._attrs[str(key)] = str(value)
        return self

    def reads_from(self, *sources: "Span | str", tool: str = "read") -> "Span":
        """Declare that these agents' outputs flowed INTO this step.

        The second inbound edge. Span nesting gives a step exactly one
        predecessor — its parent — so a joiner that merged three workers
        reconstructs with no link to any of them, and blame stops at the join.
        Every extra flow has to be stated, and this is how::

            with r.span("merge") as m:
                m.reads_from(w1, w2, w3)      # Span objects, or agent names
                m.output = document

        Accepts a :class:`Span` (same run) or a plain agent NAME, so a joiner in
        another process can name the peers it collected from — as long as their
        runs reach the reconstruction in the same batch, otherwise the edge is
        dropped rather than invented.

        Prefer :meth:`Run.join`, which also carries the sources' outputs into
        the joiner's input. Use this directly when the join is not a separate
        step (a step that reads one extra input beside its parent's).
        """
        for source in sources:
            name = source.name if isinstance(source, Span) else str(source or "").strip()
            if not name:
                continue
            if name == self.name:
                # Reconstruction resolves the target by NAME. A source sharing
                # this span's name addresses either this run (dropped as a
                # self-edge) or a same-named sibling picked by start time — a
                # confident edge to the wrong node. Say nothing instead.
                logger.warning(
                    "detective_sdk: %r reads_from its own agent name; no edge recorded", name
                )
                continue
            self._run._delegate(
                self, name, tool=tool, target=source if isinstance(source, Span) else None
            )
        return self

    # -- context manager ----------------------------------------------------- #

    def end(self, output: Any = None) -> "Span":
        """Close the step explicitly.

        Existing systems rarely let you wrap code in a ``with`` block — you hook
        whatever callbacks the framework already fires (``on_chain_start`` /
        ``on_chain_end``, a phase event bus, a task listener). Then you open the
        step in one callback and end it in another. Idempotent, so a framework
        that fires its "finished" callback twice cannot corrupt the timing.
        """
        if self._end_ns is not None:
            return self  # already closed: a repeated callback changes nothing
        if output is not None:
            self.output = output
        self._end_ns = time.time_ns()
        self._run._close(self)
        return self

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, tb: TracebackType | None) -> bool:
        if exc_type is not None:
            self._status = "error"
            if self.output is None:
                self.output = f"{exc_type.__name__}: {exc}"
        self.end()
        return False  # never swallow the caller's exception

    # -- export -------------------------------------------------------------- #

    def _to_otlp(self, trace_id: str, end_ns: int, name: str | None = None) -> dict:
        # `name` is the disambiguated agent name (see Run._final_names); it
        # differs from self.name only when siblings collided.
        name = name or self.name
        attributes = [
            # Without AGENT the span never becomes a node: framework
            # auto-instrumentation emits CHAIN, which Agent Detective ignores.
            _attr("openinference.span.kind", "AGENT"),
            _attr("gen_ai.agent.name", name),
            _attr("input.value", _encode(self._input)),
            _attr("output.value", _encode(self.output)),
        ]
        attributes += [_attr(k, v) for k, v in sorted(self._attrs.items())]
        return {
            "traceId": trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_id,
            "name": name,
            "kind": 1,
            "startTimeUnixNano": str(self._start_ns),
            "endTimeUnixNano": str(self._end_ns or end_ns),
            "attributes": attributes,
            "status": {"code": 2 if self._status == "error" else 1},
        }


class Retry:
    """A retry loop: per-attempt identity, and the back-edge that closes it.

    Opened by :meth:`Run.retry`. Two things have to be true before a loop is
    visible as a loop, and neither happens by itself:

    1. **Per-attempt identity.** Reconstruction emits no edge at all between
       spans that share a ``gen_ai.agent.name``, so three attempts at ``write``
       are three disconnected nodes. :meth:`attempt` names them ``write#1``,
       ``write#2``, ``write#3`` and chains each one behind the last, which is
       also the truth about the data: attempt N+1 read attempt N's result.
    2. **The back-edge.** A chain of attempts is still a chain. What closes it
       is the flow the controller consumed — the last attempt's output going
       back to the code that decided whether to loop again. That edge is emitted
       on close, and it is the reason the loop condenses into one cycle instead
       of a long line.

    The back-edge is emitted ONLY when some agent actually ran twice. A loop
    whose body ran once did not iterate, and drawing a cycle around a single
    pass would invent structure that never executed.

    Every edge here is stamped after the run it names has ended, so the loop
    holds together in time as well as in shape.

    One thing to know about what the analysis then reports: the loop-anomaly
    check counts the NODES in the cycle, not the rounds. Five rounds of a
    write/qa body is a cycle of eleven nodes (ten attempts plus the controller)
    and is reported as eleven iterations. The true count is on every attempt
    span as ``agent_detective.attempt``; nothing reads it yet.
    """

    def __init__(self, run: "Run", span: Span) -> None:
        self._run = run
        self.span = span
        self._counts: dict[str, int] = {}
        self._attempts: list[Span] = []
        self._closed = False

    # -- the loop body --------------------------------------------------------- #

    def attempt(self, agent: str, *, input: Any = None) -> Span:
        """One pass at ``agent``, numbered: ``write#1``, ``write#2``, ...

        The first attempt hangs off the controller and starts from the loop's
        input; each later one hangs off the previous attempt and starts from
        THAT attempt's output — the feedback the retry was reacting to. Without
        the handoff blame has nothing to compare between one attempt and the
        next.

        An attempt whose predecessor recorded no output records no input either.
        Substituting the loop's input would claim a handoff that did not happen,
        and a contract check reading it would see the original parameters intact
        on work that never received them.
        """
        number = self._counts.get(agent, 0) + 1
        self._counts[agent] = number
        previous = self._attempts[-1] if self._attempts else None
        parent = previous.span_id if previous is not None else self.span.span_id
        if input is None:
            input = previous.output if previous is not None else self.span._input
        span = Span(self._run, f"{agent}{ATTEMPT_SEPARATOR}{number}", parent, input=input)
        # The numbered name is the node identity; these keep the fact that the
        # attempts belong to ONE agent, which the name alone no longer carries.
        # Nothing reads them today — they exist so a later baseline does not
        # have to re-derive them by splitting a string.
        span._attrs["agent_detective.attempt"] = str(number)
        span._attrs["agent_detective.attempt_of"] = str(agent)
        self._run._track(span)
        self._attempts.append(span)
        return span

    def end(self, agent: str, *, output: Any = None, failed: bool = False) -> Optional[Span]:
        """End the open attempt at ``agent`` — the event-driven twin of
        :meth:`attempt`, for frameworks that fire start and finish separately.

        Takes the plain agent name (``"write"``), not the numbered one: the
        callback that reports the finish does not know which attempt it was.
        Returns ``None`` for an attempt that was never opened.
        """
        prefix = f"{agent}{ATTEMPT_SEPARATOR}"
        for span in reversed(self._attempts):
            if span._end_ns is not None:
                continue
            if span.name.startswith(prefix):
                if failed:
                    span.fail()
                span.end(output)
                return span
        return None

    # -- the controller's own record ------------------------------------------- #

    @property
    def output(self) -> Any:
        return self.span.output

    @output.setter
    def output(self, value: Any) -> None:
        self.span.output = value

    def cost(self, **kwargs: Any) -> "Retry":
        """Spend of the loop controller itself (its dispatch/decide calls)."""
        self.span.cost(**kwargs)
        return self

    # -- closing ---------------------------------------------------------------- #

    def close(self, output: Any = None) -> "Retry":
        """End the loop: emit the back-edge, then close the controller."""
        if self._closed:
            return self
        self._closed = True
        # Close the attempts first. The back-edge must be stamped after the run
        # it names has ended, or the trace claims a delegation that finished
        # before its target started — an edge no execution could produce, which
        # reconstruction will nonetheless happily turn into a cycle.
        for span in reversed(self._attempts):
            if span._end_ns is None:
                span.end()
        repeated = max(self._counts.values(), default=0) >= 2
        if repeated and self._attempts:
            # The flow that makes it a loop: the controller read the last
            # attempt's result. Only ONE back-edge — the controller consumed
            # every attempt, but one returning flow already closes the cycle,
            # and an edge per attempt would render the loop as a mesh.
            self.span.reads_from(self._attempts[-1], tool="loop_result")
        self.span.end(output)
        return self

    def __enter__(self) -> "Retry":
        return self

    def __exit__(self, exc_type, exc, tb: TracebackType | None) -> bool:
        if exc_type is not None:
            self.span._status = "error"
            if self.span.output is None:
                self.span.output = f"{exc_type.__name__}: {exc}"
        self.close()
        return False  # never swallow the caller's exception


class Run:
    """One end-to-end run: the root node plus every step under it.

    The root carries the ORIGINAL request (``task``). That is the provenance the
    terminal judge cites — without it there is nothing to check the deliverable
    against. The root has no output of its own; that is expected of a wrapper
    and Agent Detective records it as unscored rather than as a failure.
    """

    def __init__(
        self,
        name: str = "run",
        *,
        task: Any = None,
        service: str | None = None,
        endpoint: str | None = None,
        trace_file: str | None = None,
        parent_span_id: str | None = None,
        trace_id: str | None = None,
        graph_id: str | None = None,
    ) -> None:
        self.name = name
        self._task = task
        self._service = service or os.getenv(_SERVICE_ENV) or name
        endpoint = endpoint if endpoint is not None else os.getenv(_ENDPOINT_ENV)
        trace_file = trace_file if trace_file is not None else os.getenv(_TRACE_FILE_ENV)
        self._endpoint = (endpoint or "").strip().rstrip("/") or None
        self._trace_file = (trace_file or "").strip() or None
        self.enabled = bool(self._endpoint or self._trace_file)

        self._trace_id = _trace_id_of(trace_id)
        self._root_id = _hex(8)
        self._parent_span_id = _span_id(parent_span_id)
        self._attrs: dict[str, str] = {}
        if graph_id is not None and str(graph_id).strip():
            self.attr("x-execution-graph-id", graph_id)
        self._start_ns = time.time_ns()
        self._spans: list[Span] = []
        self._open: list[Span] = []          # nesting stack, for `span`
        self._last_step: Optional[Span] = None  # handoff chain, for `step`
        self._closed = False
        # Parallel arms are the point of `branch` and `retry(parallel=True)`, so
        # two threads really do open and close spans at the same time. `_close`
        # REBUILDS the open list, and a rebuild racing an append drops the
        # appended span from the stack — a lost parent, silently, in the one
        # shape this SDK exists to get right.
        self._lock = threading.RLock()

    @property
    def trace_id(self) -> str:
        """This run's trace id, for handing to the next process in the pipeline."""
        return self._trace_id

    @property
    def root_span_id(self) -> str:
        """Span id of the run root; pass as ``parent_span_id`` to a downstream run."""
        return self._root_id

    def attr(self, key: str, value: Any) -> "Run":
        """Attribute on the run's root span; last write wins, overrides the fixed set."""
        self._attrs[str(key)] = _encode(value)
        return self

    def _track(self, span: "Span") -> None:
        """Record a span and push it on the open stack."""
        with self._lock:
            if self.enabled:
                self._spans.append(span)
            self._open.append(span)

    # -- opening steps -------------------------------------------------------- #

    def step(self, name: str, *, input: Any = None) -> Span:
        """A step in a PIPELINE: its parent is the previous step.

        When ``input`` is omitted it defaults to the previous step's output —
        the handoff really did carry that value, and without it every node looks
        like it started from nothing, leaving blame nothing to compare.
        """
        with self._lock:
            last = self._last_step
            parent = last.span_id if last is not None else self._root_id
            if input is None:
                input = last.output if last is not None else self._task
            span = Span(self, name, parent, input=input)
            self._track(span)
            self._last_step = span
        return span

    def span(self, name: str, *, input: Any = None) -> Span:
        """A NESTED step: its parent is the innermost span still open."""
        with self._lock:
            parent = self._open[-1].span_id if self._open else self._root_id
            if input is None:
                input = self._open[-1]._input if self._open else self._task
            span = Span(self, name, parent, input=input)
            self._track(span)
        return span

    def _fanout_point(self) -> Optional[Span]:
        """The step parallel work hangs off: innermost open span, else the chain
        head, else the root (``None``).

        Open BRANCHES are skipped. Real parallel work is open all at once — three
        threads, three still-running arms — and taking the innermost open span
        would then chain arm 2 behind arm 1 and produce a pipeline nobody ran.
        """
        with self._lock:
            for span in reversed(self._open):
                if not span._parallel:
                    return span
            return self._last_step

    def branch(self, name: str, *, input: Any = None, of: Span | None = None) -> Span:
        """One arm of a FAN-OUT: parallel with its siblings, not chained to them.

        ``step`` would chain the arms into a false pipeline (arm 2 reading arm
        1's output) and ``span`` would hang them off the run root, losing the
        edge from the step that dispatched them. A branch parents on the
        fan-out point — the innermost open span, else the current chain head —
        so the dispatcher really is each arm's predecessor, and its input
        defaults to what that step produced.

        Branches deliberately do NOT become the chain head: a following ``step``
        continues from the fan-out point, not from whichever arm happened to run
        last. Merge them with :meth:`join`.

        Arms may be open at the same time (they usually are — that is what
        parallel means); a branch never parents on another branch. Pass ``of=``
        to fan out from a specific step, e.g. a second fan-out nested inside an
        arm that is still running.
        """
        with self._lock:
            point = of if of is not None else self._fanout_point()
            parent = point.span_id if point is not None else self._root_id
            if input is None and point is not None:
                # ONLY what the dispatcher actually produced. When it is still open
                # and has produced nothing yet, the arm's input stays unknown —
                # substituting the dispatcher's OWN input would claim a handoff that
                # did not happen, and a contract check reading it would find the
                # original parameters intact on work that never received them. This
                # is the refusal `Retry.attempt` already makes; `branch` inherited
                # the opposite habit from `span` and it was wrong in both.
                input = point.output
            if input is None and point is None:
                input = self._task
            span = Span(self, name, parent, input=input)
            span._parallel = True
            self._track(span)
        return span

    def join(self, name: str, sources: "Iterable[Span | str]", *, input: Any = None) -> Span:
        """The FAN-IN: one step that merges what several others produced.

        The shape span nesting cannot express. Each source contributes an edge
        source -> join (see :meth:`Span.reads_from`), so blame can walk back
        from a bad merge into the arm that poisoned it instead of stopping at
        the joiner.

        ``input`` defaults to ``{agent name: that agent's output}`` — the joiner
        really did receive those values, and without them the merge step looks
        like it invented its result. A bare agent name resolves to the spans of
        that name already recorded in this run, so an event-driven fan-in (the
        integrator holds callbacks, not :class:`Span` objects) merges exactly
        what the ``with``-style one does. Sources the SDK never saw an output
        for (an unknown name, or a span whose ``output`` was never set) are
        LEFT OUT of that default rather than entered as null: null reads as
        "produced nothing", which is a different claim from "not recorded here".
        Pass ``input=`` when you have the real merged input.
        """
        collected = list(sources)
        with self._lock:
            point = self._fanout_point()
            parent = point.span_id if point is not None else self._root_id
            if input is None:
                input = self._join_input(collected)
            span = Span(self, name, parent, input=input)
            self._track(span)
            span.reads_from(*collected, tool="collect")
            # The pipeline resumes here: a following `step` continues from the
            # merge, not from the step that fanned out.
            self._last_step = span
        return span

    def _join_input(self, sources: "list[Span | str]") -> Any:
        """Default joiner input: only the source outputs actually observed.

        Same-named arms are GROUPED, never overwritten. Keying a flat dict by
        agent name silently dropped every arm but the last of a map-reduce
        fan-in — three arms called ``worker`` left one entry holding arm #3's
        text, presented as the complete merge input. A judge then scored the
        merge against a third of what it merged, and blame walking back from a
        bad merge had two thirds of the evidence deleted.

        A bare name stands for every span this run recorded under it, so the
        same-named-arm grouping applies whether the sources arrived as objects
        or as names.
        """
        grouped: dict[str, list[Any]] = {}
        with self._lock:
            for source in sources:
                if isinstance(source, Span):
                    if source.output is not None:
                        grouped.setdefault(source.name, []).append(source.output)
                    continue
                name = str(source or "").strip()
                if not name:
                    continue
                for span in self._spans:
                    if (
                        isinstance(span, Span)
                        and span.name == name
                        and span.output is not None
                    ):
                        grouped.setdefault(name, []).append(span.output)
        merged = {
            name: outputs[0] if len(outputs) == 1 else outputs
            for name, outputs in grouped.items()
        }
        # Empty stays absent, not `{}` — an empty merge input would claim the
        # joiner was handed nothing.
        return merged or None

    def retry(
        self,
        name: str,
        *,
        input: Any = None,
        of: Span | None = None,
        parallel: bool = False,
    ) -> "Retry":
        """A LOOP: repeated attempts at the same work, under one controller.

        ``name`` names the controller — the code that dispatches an attempt,
        reads the result and decides to go again. It is a real node: the
        attempts' outputs flow back into it, and that returning flow is what
        makes the loop a loop rather than a long chain.

        The controller hangs off the enclosing step (the innermost open span,
        else the current chain head) and starts from what that step produced.
        Pass ``of=`` to name that step yourself.

        ``parallel=True`` makes this loop one ARM of a fan-out — the same thing
        :meth:`branch` declares, for a loop instead of a single step. Two arms
        running in threads, one of them iterative, is a shape the catalogue is
        full of (a research loop beside a drafting loop, joined at the end) and
        it was not expressible: whichever arm opened its span first became the
        other one's parent, so the trace claimed a handoff no execution ever
        performed. A parallel loop never parents on another arm, and no later
        arm parents on it.

        Set ``loop.output`` to what the loop finally returned. It is left unset
        otherwise — the SDK will not assume the last attempt's output was
        accepted, and an unset output is recorded as unscored, not as empty.
        """
        with self._lock:
            if of is not None:
                point = of
            elif parallel:
                point = self._fanout_point()
            else:
                point = self._open[-1] if self._open else self._last_step
            parent = point.span_id if point is not None else self._root_id
            if input is None:
                input = point.output if point is not None else self._task
            span = Span(self, name, parent, input=input)
            span._parallel = parallel
            self._track(span)
            if not parallel:
                # The loop is one stage of the enclosing pipeline: what follows
                # it chains off the controller, never off an attempt. An ARM is
                # not a stage — like `branch`, it must not become the chain head,
                # or the step after the join would continue from whichever arm
                # ran last.
                self._last_step = span
        return Retry(self, span)

    def _delegate(
        self, span: Span, target_name: str, *, tool: str, target: Span | None = None
    ) -> None:
        """Record one ``source -> span`` flow as a TOOL delegation span."""
        record = _Delegation(tool, span.span_id, target_name, target)
        with self._lock:
            if self.enabled:
                self._spans.append(record)

    def _final_names(self) -> dict[int, str]:
        """``id(span) -> the agent name that ships``, disambiguating collisions.

        Reconstruction resolves an edge target by NAME, so two runs sharing one
        name are indistinguishable to it: the canonical map-reduce fan-out (three
        arms all called ``worker``) collapsed its three edges onto whichever arm
        started first, and the graph then claimed arm #1 fed the merge while the
        payload carried arm #3's text.

        Same remedy :class:`Retry` already uses for attempts — number them —
        applied only where a collision actually exists, so an integrator who
        chose distinct names sees the names they chose. Renaming happens HERE and
        not at ``branch()`` time because only the finished run knows whether a
        second arm ever showed up.
        """
        by_name: dict[str, list[Span]] = {}
        for span in self._spans:
            if isinstance(span, Span):
                by_name.setdefault(span.name, []).append(span)
        names: dict[int, str] = {}
        for name, spans in by_name.items():
            if len(spans) == 1:
                names[id(spans[0])] = name
                continue
            for number, span in enumerate(
                sorted(spans, key=lambda s: (s._start_ns, s.span_id)), start=1
            ):
                names[id(span)] = f"{name}{ATTEMPT_SEPARATOR}{number}"
        return names

    def end(self, name: str, *, output: Any = None, failed: bool = False) -> Optional[Span]:
        """End the still-open step called ``name`` (the most recent one).

        The event-driven half of :meth:`step` — for existing systems, where the
        framework fires "started" and "finished" as separate callbacks and there
        is no block to wrap::

            def on_phase_start(step):          r.step(step)
            def on_phase_finish(step, result): r.end(step, output=result)

        Returns ``None`` for a step that was never opened (a stray "finished"
        callback), rather than inventing a zero-length span.
        """
        with self._lock:
            for span in reversed(self._open):
                if span.name == name:
                    if failed:
                        span.fail()
                    span.end(output)
                    return span
        return None

    def _close(self, span: Span) -> None:
        # Spans can close out of order (concurrency); drop by identity, not by
        # popping blindly, or one late finisher would unbalance the stack.
        with self._lock:
            self._open = [s for s in self._open if s is not span]

    # -- context manager ------------------------------------------------------ #

    def __enter__(self) -> "Run":
        return self

    def __exit__(self, exc_type, exc, tb: TracebackType | None) -> bool:
        self.close()
        return False

    def close(self) -> None:
        """Export the collected trace. Best-effort and idempotent."""
        if self._closed or not self.enabled:
            self._closed = True
            return
        self._closed = True
        if not self._spans:
            return
        try:
            payload = self.build_payload()
        except Exception:  # noqa: BLE001
            logger.debug("detective_sdk: build_payload failed", exc_info=True)
            return
        self._deliver(payload)

    def _check_delegations(self) -> None:
        """Warn about declared flows that will not become edges.

        Both failure modes are silent downstream — you get a graph with a
        missing or a misdirected edge, never an error — so they have to be said
        here, where the names are still in hand. Reconstruction resolves a
        delegation target by agent NAME: two spans carrying it means the edge
        lands on whichever ran first, and a name this run never used means the
        edge exists only if some other exporter delivers that run in the same
        batch (a cross-process join) — or, more often, that the name is a typo.
        """
        counts: dict[str, int] = {self.name: 1}
        for span in self._spans:
            if isinstance(span, Span):
                counts[span.name] = counts.get(span.name, 0) + 1
        for record in self._spans:
            if not isinstance(record, _Delegation):
                continue
            if record.target_span is not None:
                continue  # addressed by object: `build_payload` resolves it exactly
            if counts.get(record.target_name, 0) == 0:
                # info, not warning: a cross-process join names a peer this
                # exporter never saw, and that is a supported case.
                logger.info(
                    "detective_sdk: no agent named %r in this run; that edge needs "
                    "the run to arrive from elsewhere",
                    record.target_name,
                )

    def build_payload(self) -> dict:
        """The OTLP ``ExportTraceServiceRequest`` (JSON encoding)."""
        self._check_delegations()
        end_ns = time.time_ns()
        attrs = [
            _attr("openinference.span.kind", "AGENT"),
            _attr("gen_ai.agent.name", self.name),
            _attr("input.value", _encode(self._task)),
            _attr("output.value", ""),
        ]
        if self._attrs:
            by_key = {entry["key"]: entry for entry in attrs}
            for key, value in self._attrs.items():
                entry = by_key.get(key)
                if entry is None:
                    entry = _attr(key, value)
                    attrs.append(entry)
                    by_key[key] = entry
                else:
                    entry["value"]["stringValue"] = value
        root = {
            "traceId": self._trace_id,
            "spanId": self._root_id,
            "parentSpanId": self._parent_span_id,
            "name": self.name,
            "kind": 1,
            "startTimeUnixNano": str(self._start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": attrs,
            "status": {"code": 1},
        }
        names = self._final_names()
        ambiguous = {n for n, c in _counted(names.values()).items() if c > 1}
        spans = [root]
        for item in self._spans:
            if isinstance(item, Span):
                spans.append(item._to_otlp(self._trace_id, end_ns, names.get(id(item))))
                continue
            target = item.resolved_name(names)
            if not target or target in ambiguous:
                # A bare name we cannot pin to one run. An edge to the wrong node
                # is a confident lie; no edge is merely a gap. Same choice
                # `reads_from` makes when a source names the reader's own agent.
                logger.warning(
                    "detective_sdk: delegation target %r matches no single run; no edge recorded",
                    item.target_name,
                )
                continue
            # A delegation stamped before the run it names has ENDED is a causally
            # impossible edge, and reconstruction will happily build a cycle out
            # of one. Push it past its target's end.
            target_end = (
                item.target_span._end_ns
                if item.target_span is not None and item.target_span._end_ns is not None
                else 0
            )
            spans.append(
                item._to_otlp(
                    self._trace_id, end_ns, target, max(item._created_ns, target_end)
                )
            )
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attr("service.name", self._service)]},
                    "scopeSpans": [{"scope": {"name": "detective_sdk"}, "spans": spans}],
                }
            ]
        }

    def _deliver(self, payload: dict) -> None:
        deliver(payload, endpoint=self._endpoint, trace_file=self._trace_file)


def deliver(
    payload: dict, *, endpoint: str | None = None, trace_file: str | None = None
) -> None:
    """Write and/or POST an OTLP payload. Best-effort; never raises.

    Shared with :mod:`detective_sdk.otel`, so a hand-built run and a bridged
    OpenTelemetry run leave by exactly the same door.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if trace_file:
        try:
            with open(trace_file, "w", encoding="utf-8") as handle:
                handle.write(body.decode("utf-8"))
        except Exception as err:  # noqa: BLE001
            logger.warning("detective_sdk: writing %s failed: %s", trace_file, err)
    if not endpoint:
        return
    url = f"{endpoint.rstrip('/')}/v1/traces"
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except Exception as err:  # noqa: BLE001 - export never takes the run down
        logger.warning("detective_sdk: export to %s failed: %s", url, err)


def run(
    name: str = "run",
    *,
    task: Any = None,
    service: str | None = None,
    endpoint: str | None = None,
    trace_file: str | None = None,
    parent_span_id: str | None = None,
    trace_id: str | None = None,
    graph_id: str | None = None,
) -> Run:
    """Start a run. Use as a context manager; the trace exports on exit."""
    return Run(
        name,
        task=task,
        service=service,
        endpoint=endpoint,
        trace_file=trace_file,
        parent_span_id=parent_span_id,
        trace_id=trace_id,
        graph_id=graph_id,
    )


__all__ = [
    "run",
    "Run",
    "Span",
    "Retry",
    "deliver",
    "MAX_PAYLOAD_CHARS",
    "ATTEMPT_SEPARATOR",
]
