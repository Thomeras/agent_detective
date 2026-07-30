# LangGraph adapter example: dispatcher -> Send fan-out (3 arms) -> joiner.
#
#   python examples/langgraph_fanout.py
#   AGENT_DETECTIVE_ENDPOINT=http://localhost:8001 python examples/langgraph_fanout.py
#
# The instrumentation is the three lines around `app.invoke` — the graph
# itself is plain LangGraph and the nodes are offline stubs.

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from detective_sdk.langgraph import DetectiveLangGraphHandler

TASK = "Draft the launch announcement in three tones and merge the best lines."


class State(TypedDict):
    task: str
    drafts: Annotated[list[str], operator.add]
    final: str


def dispatcher(state: State) -> dict:
    return {"drafts": []}


def pick_tones(state: State) -> list[Send]:
    return [Send("writer", {**state, "tone": tone}) for tone in ("formal", "bold", "playful")]


def writer(state: State) -> dict:
    tone = state.get("tone", "formal")
    return {"drafts": [f"[{tone}] Atlas Sync 2.0 is here — two-way sync, 20x faster."]}


def merger(state: State) -> dict:
    return {"final": " ".join(state["drafts"])}


def build_app():
    graph = StateGraph(State)
    graph.add_node("dispatcher", dispatcher)
    graph.add_node("writer", writer)
    graph.add_node("merger", merger)
    graph.add_edge(START, "dispatcher")
    graph.add_conditional_edges("dispatcher", pick_tones)
    graph.add_edge("writer", "merger")
    graph.add_edge("merger", END)
    return graph.compile()


def main() -> None:
    app = build_app()

    handler = DetectiveLangGraphHandler("launch-announcement")
    result = app.invoke({"task": TASK, "drafts": [], "final": ""}, config={"callbacks": [handler]})
    handler.close()

    print(result["final"])
    if handler.run is not None and handler.run.enabled:
        print("trace exported")
    else:
        print("no AGENT_DETECTIVE_ENDPOINT / AGENT_DETECTIVE_TRACE_FILE set — nothing exported")


if __name__ == "__main__":
    main()
