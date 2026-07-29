"""Fault injection from outside the topology, at the model-response boundary.

The ground truth this corpus needs is "which node broke it", and the only way
to know that for certain is to break a chosen node on purpose. Doing it by
editing the topology would defeat the exercise twice over: the code under test
would no longer be foreign, and the edit itself would be a second, untracked
difference from the clean run.

So the fault is applied to what one agent RETURNS, in the same outside wrapper
that adds the spans — after the model answered, before the topology sees it.
agent_topo_db's own code, prompts and control flow are byte-identical between
the clean and the faulted run, which is what makes a diff of their verdicts
mean something. (Not the HTTP boundary itself: in topolab one ``Agent.run`` is
exactly one completion, so the two are the same seam, and this one also knows
which agent it is holding.)

Each injector is a pure ``(agent_name, call_index, text) -> text``. They are
listed with the report the analysis is expected to produce, which is the label
half of the corpus (see ``cases.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Protocol


class Injector(Protocol):
    def __call__(self, agent_name: str, call_index: int, text: str) -> str: ...


@dataclass(frozen=True)
class Fault:
    """One injectable fault, with the claim it is supposed to produce."""

    name: str
    target_agent: str
    apply: Callable[[str], str]
    #: What the analysis should say. The corpus asserts against these.
    expect_incident: bool = True
    note: str = ""


# --- the fault library ----------------------------------------------------- #
#
# Each of these mangles OUTPUT text only. They are deliberately crude and
# deterministic: a corpus entry has to reproduce, and "the model was asked to be
# subtly wrong" reproduces nothing.


def truncate_midway(text: str) -> str:
    """Cut the answer off mid-sentence — the shape a token limit produces."""
    if len(text) < 200:
        return text
    cut = text[: len(text) // 2]
    return cut.rsplit(" ", 1)[0]


def drop_numbers(text: str) -> str:
    """Silently remove every figure, leaving fluent prose that claims nothing.

    The nastiest realistic failure: downstream nodes stay confident because the
    text still reads like an answer.
    """
    return re.sub(r"\d[\d\s.,%]*", "", text)


def rewrite_currency(text: str) -> str:
    """Change the unit while keeping the number — a contract breach that
    propagates into the deliverable without ever looking malformed."""
    return text.replace("CZK", "EUR").replace("Kč", "€")


def empty_answer(text: str) -> str:
    """Spend the call and return nothing. Not hypothetical: a reasoning model
    burning its whole budget on reasoning does exactly this."""
    return ""


def rubber_stamp(text: str) -> str:
    """A verifier that approves whatever it was given."""
    return "OK, vse v poradku, schvaleno bez vyhrad."


def hallucinate_fact(text: str) -> str:
    """Append a confident, unsupported claim."""
    return text + (
        "\n\nPotvrzeno interním auditem z 12. brezna: vsechny hodnoty overeny "
        "proti primarnimu zdroji a schvaleny financnim reditelem."
    )


class ResponseInjector:
    """Applies one fault to the outputs of one agent.

    Handed to the span adapter, which calls it for every ``Agent.run`` return
    value — the single point all 22 topologies funnel through, so one hook
    covers them all without knowing anything about any of them.
    """

    def __init__(self, fault: Fault | None) -> None:
        self._fault = fault
        self._calls: dict[str, int] = {}
        self.applied = 0   # invocations that CHANGED the text
        self.no_ops = 0    # invocations where the fault matched nothing

    def transform(self, agent_name: str, text: str) -> str:
        index = self._calls.get(agent_name, 0)
        self._calls[agent_name] = index + 1
        if self._fault is None or agent_name != self._fault.target_agent:
            return text
        changed = self._fault.apply(text)
        if changed == text:
            # Counting INVOCATIONS was the bug: `rewrite_currency` substitutes
            # "CZK", the adapter emits the column as `trzby_czk`, the substitution
            # matched nothing — and the cell still shipped labelled as faulted.
            # A recorded fault that changed no text is a clean run wearing a
            # ground truth that is false, which is worse than no cell at all.
            self.no_ops += 1
            return text
        self.applied += 1
        return changed


FAULTS: dict[str, Callable[[str], str]] = {
    "truncate": truncate_midway,
    "drop_numbers": drop_numbers,
    "rewrite_currency": rewrite_currency,
    "empty_answer": empty_answer,
    "rubber_stamp": rubber_stamp,
    "hallucinate": hallucinate_fact,
}
