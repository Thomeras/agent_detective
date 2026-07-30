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
    """

    def __init__(
        self,
        name: str = "langgraph",
        *,
        run: Optional[Run] = None,
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
        self._graph_run_id: Any = None
        self._open: dict[Any, Span] = {}  # callback run_id -> the span it opened
        self._arms: list[Span] = []       # fan-out arms not yet merged by a join
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

    # -- internals ------------------------------------------------------------ #

    def _start(self, inputs: Any, *, run_id: Any, parent_run_id: Any, metadata: Any) -> None:
        with self._lock:
            if parent_run_id is None:
                # The graph invocation itself: open the Run on the ORIGINAL request.
                if self._run is None:
                    self._run = Run(self._name, task=inputs, **self._run_kwargs)
                self._graph_run_id = run_id
                return
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
        if span is None:
            return  # the graph root or a skipped nested run
        if error is not None:
            span.fail(error)
        span.end(output)
