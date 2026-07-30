"""LangGraph adapter: the framework's callback seam -> steps/branches/joins.

LangGraph already fires a callback per node run, so a LangGraph agent needs
three lines, not a hand-written ``DetectiveRun`` wrapper::

    from detective_sdk.langgraph import DetectiveLangGraphHandler

    handler = DetectiveLangGraphHandler()
    app.invoke(state, config={"callbacks": [handler]})
    handler.close()

The mapping is mechanical: a node becomes a :meth:`Run.step`, a ``Send``
fan-out arm a :meth:`Run.branch`, the node the arms converge on a
:meth:`Run.join`, and the graph invocation the :class:`Run` itself, carrying
the original request as its task. Without ``AGENT_DETECTIVE_ENDPOINT`` /
``AGENT_DETECTIVE_TRACE_FILE`` the run is a no-op.

Importable without LangGraph installed — the framework is only needed to
instantiate the handler, so the package core stays dependency-free.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .tracing import Run, Span

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError:  # the extra is optional; fail at construction, not at import
    BaseCallbackHandler = object  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

# LangGraph marks a task dispatched via `Send` in `langgraph_triggers`; a plain
# edge shows up as `branch:to:<node>` instead.
_SEND_TRIGGER = "__pregel_push"


class DetectiveLangGraphHandler(BaseCallbackHandler):  # type: ignore[valid-type, misc]
    """Records one LangGraph run as steps, branches and joins of a single Run.

    Attaches through ``config={"callbacks": [...]}`` — the framework's
    official extension point, no monkey-patching. Only direct children of the
    graph's root callback run count as nodes: the conditional-edge router (a
    nested run reusing the node's name) and subgraph internals are skipped.

    Nodes run on framework threads, so callback state is guarded by a lock and
    anything unreadable is skipped with a log — a tracing hiccup must never
    raise into the running graph.

    LLM calls made inside a node have their token usage captured automatically
    (``on_llm_end``) and summed onto the node's span. A node's price is
    computed only when the integrator passed ``pricing`` AND every LLM call in
    that node resolved to a priced model with full token counts — a partial
    sum reads as the whole, so anything less stays unknown (``null``), never
    ``$0``.
    """

    def __init__(
        self,
        name: str = "langgraph",
        *,
        run: Optional[Run] = None,
        pricing: "Optional[dict[str, tuple[float, float]]]" = None,
        **run_kwargs: Any,
    ) -> None:
        if BaseCallbackHandler is object:
            raise ImportError(
                "detective_sdk.langgraph needs langchain-core; "
                "install it with: pip install 'detective-sdk[langgraph]'"
            )
        super().__init__()
        self._name = name
        self._run = run
        self._run_kwargs = run_kwargs
        # Integrator-supplied price list: model name -> (input, output) USD per
        # 1M tokens. Optional by design — without it token counts are still
        # recorded and the node's cost stays honestly unknown, never $0.
        self._pricing = pricing or {}
        self._graph_run_id: Any = None
        self._open: dict[Any, Span] = {}  # callback run_id -> the span it opened
        self._arms: list[Span] = []       # fan-out arms not yet merged by a join
        self._parents: dict[Any, Any] = {}  # nested run_id -> parent run_id
        self._usage: dict[Any, list] = {}  # node run_id -> [(tokens_in, tokens_out, model)]
        self._lock = threading.Lock()

    @property
    def run(self) -> Optional[Run]:
        """The run being recorded; exists once the graph invocation started."""
        return self._run

    def close(self) -> None:
        """Export the trace. Best-effort; safe to call more than once."""
        run = self._run
        if run is not None:
            run.close()

    # -- chain callbacks (LangGraph nodes are chains) ------------------------- #

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._start(inputs, run_id=run_id, parent_run_id=parent_run_id, metadata=metadata)
        except Exception:  # noqa: BLE001 - never raise into the running graph
            logger.warning("detective_sdk.langgraph: chain start skipped", exc_info=True)

    def on_chain_end(self, outputs: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        try:
            self._finish(run_id, output=outputs)
        except Exception:  # noqa: BLE001
            logger.warning("detective_sdk.langgraph: chain end skipped", exc_info=True)

    def on_chain_error(self, error: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        try:
            self._finish(run_id, error=error)
        except Exception:  # noqa: BLE001
            logger.warning("detective_sdk.langgraph: chain error skipped", exc_info=True)

    # -- llm callbacks (usage capture) ---------------------------------------- #

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._capture_usage(response, parent_run_id)
        except Exception:  # noqa: BLE001 - usage is a bonus, never a condition
            logger.warning("detective_sdk.langgraph: usage capture skipped", exc_info=True)

    # -- internals ------------------------------------------------------------ #

    def _capture_usage(self, response: Any, parent_run_id: Any) -> None:
        """Add one LLM call's usage to the node it ran inside.

        The call may sit several nested runs below the node, so the parent
        chain recorded in ``_parents`` is walked until an open node span turns
        up. Calls outside any node (e.g. directly under the graph root) have
        no step to charge and are skipped.
        """
        with self._lock:
            owner = parent_run_id
            while owner is not None and owner not in self._open:
                owner = self._parents.get(owner)
            if owner is None:
                return
            usage = _usage_of(response)
            if usage is not None:
                self._usage.setdefault(owner, []).append(usage)

    def _apply_usage(self, span: Span, calls: "list[tuple[int | None, int | None, str | None]]") -> None:
        """Charge a node's LLM calls to its span.

        A count is summed only when EVERY call reported it and a price is
        computed only when every call has full counts and a priced model: a
        partial figure reads as the node's whole spend, which is a confident
        lie — unknown stays ``null``.
        """
        tokens_in = _sum_if_complete(c[0] for c in calls)
        tokens_out = _sum_if_complete(c[1] for c in calls)
        models = {c[2] for c in calls if c[2]}
        usd = None
        if self._pricing and tokens_in is not None and tokens_out is not None:
            priced = [self._pricing.get(c[2] or "") for c in calls]
            if all(p is not None for p in priced):
                usd = sum(
                    (c[0] or 0) * p[0] / 1_000_000 + (c[1] or 0) * p[1] / 1_000_000  # type: ignore[misc]
                    for c, p in zip(calls, priced)
                )
        span.cost(
            usd=usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=models.pop() if len(models) == 1 else None,
        )

    def _start(self, inputs: Any, *, run_id: Any, parent_run_id: Any, metadata: Any) -> None:
        with self._lock:
            if parent_run_id is None:
                # The graph invocation itself: open the Run on the ORIGINAL request.
                if self._run is None:
                    self._run = Run(self._name, task=inputs, **self._run_kwargs)
                self._graph_run_id = run_id
                return
            # Recorded for EVERY nested run so usage capture can walk from an
            # LLM callback up to the node span that owns it.
            self._parents[run_id] = parent_run_id
            if self._run is None or parent_run_id != self._graph_run_id:
                return  # nested run: router, subgraph internals, LLM/tool wrappers
            node = str((metadata or {}).get("langgraph_node") or "").strip()
            if not node:
                logger.debug("detective_sdk.langgraph: chain run without a node name; skipped")
                return
            triggers = (metadata or {}).get("langgraph_triggers") or ()
            if _SEND_TRIGGER in triggers:
                span = self._run.branch(node, input=inputs)
                self._arms.append(span)
            elif self._arms:
                # The first node a fan-out converges on is the fan-in; it really
                # did receive the arms' outputs, so the edges and the join input
                # come from the arm spans themselves (same-named arms included).
                span = self._run.join(node, self._arms, input=inputs)
                self._arms = []
            else:
                span = self._run.step(node, input=inputs)
            self._open[run_id] = span

    def _finish(self, run_id: Any, *, output: Any = None, error: Any = None) -> None:
        with self._lock:
            span = self._open.pop(run_id, None)
            calls = self._usage.pop(run_id, None)
            self._parents.pop(run_id, None)
        if span is None:
            return  # the graph root or a skipped nested run
        if calls:
            self._apply_usage(span, calls)
        if error is not None:
            span.fail(error)
        span.end(output)


def _usage_of(response: Any) -> "tuple[int | None, int | None, str | None] | None":
    """(tokens_in, tokens_out, model) read from an LLMResult, None if unreadable.

    Usage arrives in different shapes across providers and langchain versions:
    ``message.usage_metadata`` (current standard), ``llm_output["token_usage"]``
    (OpenAI-style), or ``message.response_metadata`` (Anthropic-style). Every
    known shape is tried; a missing leg stays ``None`` — an unread count is
    unknown, not zero.
    """
    message = None
    try:
        message = response.generations[0][0].message
    except (AttributeError, IndexError, TypeError):
        pass
    usage_metadata = getattr(message, "usage_metadata", None)
    response_metadata = getattr(message, "response_metadata", None)
    llm_output = getattr(response, "llm_output", None)
    if not any(isinstance(d, dict) for d in (usage_metadata, response_metadata, llm_output)):
        return None

    tokens_in = tokens_out = None
    model = None
    if isinstance(usage_metadata, dict):
        tokens_in = _int(usage_metadata.get("input_tokens"))
        tokens_out = _int(usage_metadata.get("output_tokens"))
    for source in (llm_output, response_metadata):
        if not isinstance(source, dict):
            continue
        # OpenAI-style token_usage, or the provider's raw `usage` block.
        usage = source.get("token_usage") or source.get("usage")
        if isinstance(usage, dict):
            tokens_in = tokens_in if tokens_in is not None else _int(
                usage.get("prompt_tokens", usage.get("input_tokens"))
            )
            tokens_out = tokens_out if tokens_out is not None else _int(
                usage.get("completion_tokens", usage.get("output_tokens"))
            )
        model = model or source.get("model_name") or source.get("model")
    return tokens_in, tokens_out, str(model) if model else None


def _sum_if_complete(values: Any) -> "int | None":
    """Sum only when every value is present; one gap makes the total unknown."""
    collected = list(values)
    if any(v is None for v in collected):
        return None
    return sum(collected)


def _int(value: Any) -> "int | None":
    """Coerce a reported token count; bools and non-numbers are not counts."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)
