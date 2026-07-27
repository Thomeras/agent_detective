"""``detective doctor`` — a PRE-FLIGHT check on instrumentation, never a verdict.

Bad instrumentation fails **silently**. An app once shipped status pings
(``{"ok": true, "step": "collect"}``) where node outputs should have been:
every node got a score, the report stayed confident, and the thing it was
confident about was a progress bar. It took a whole debugging session to see
it, because nothing downstream can see it — by the time the analysis runs, the
evidence is whatever was captured, and *what instrumentation did not capture at
run time, no later analysis can manufacture* (``docs/trace-requirements.md``).

So this command runs BEFORE anyone trusts a report. It reads the trace
``detective analyze`` would read, through the same mapper, and answers one
question per check: **what can this trace support, and what can it therefore
never say.** Every finding states the CONSEQUENCE ("cost stays unknown",
"per-node quality cannot be judged from this") and a concrete fix — "0 edges"
on its own is a fact nobody can act on.

Three deliberate non-goals:

- **No verdict.** The doctor never scores a node, never names a culprit, and
  never runs a judge. Reporting "looks fine" about *quality* from a trace whose
  payloads are unjudgeable is the failure mode this exists to prevent.
- **No gate.** The exit code is 0 whatever it finds. A diagnostic that fails
  builds gets deleted from CI, and then nobody runs it at all.
- **No re-parsing.** Runs, edges, payloads and cost come from ``otel_mapper``,
  so what the doctor reports about a trace is what the analysis will actually
  see. The one raw thing it reads itself is ``openinference.span.kind``, and
  only because its whole job includes the spans that never became runs.
- **No guessing.** Where the trace cannot settle a question the answer is
  "cannot tell", printed as such — see :func:`classify_payload` and
  :class:`Claim`. An earlier version guessed in both directions: it called a
  router's ``{"action": "escalate_to_legal"}`` a broken payload, and it called
  a deliverable reading ``"ok"`` artifact text.

Every coverage number counts the runs the analysis counts. The one exception is
the payload-shaped checks, which leave out a *wrapper root* — a root span with
no output of its own — and then NAME it and say the analysis still reports it
unscored (:func:`_producing_runs`). A denominator that quietly shrinks is how a
previous version certified "cost 5/5" on a six-run trace.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from otel_mapper import flatten_export_request, map_spans, run_id_from_key
from worker.graph_ops import deliverable_run
from worker.scoring import opaque_artifact_refs
from worker.types import GraphBundle, RunRecord

from .bundle import bundles_from_mapping
from .render import Painter

Level = Literal["ok", "warn", "gap"]

# "gap" rather than "fail": nothing here failed — the trace simply cannot carry
# the claim, which is an evidence gap, not a defect in the agent system.
_LEVEL_TONE: dict[str, str] = {"ok": "ok", "warn": "warn", "gap": "fail"}
_LEVEL_RANK: dict[str, int] = {"ok": 0, "warn": 1, "gap": 2}

AGENT_KIND_ATTRIBUTE = "openinference.span.kind"


@dataclass(frozen=True)
class Check:
    """One instrumentation fact, with what it costs and how to fix it.

    ``consequence`` and ``fix`` are empty only at level ``ok``. A finding
    without a consequence is trivia — the reader has to be told what the
    analysis can no longer say, not merely what is missing.
    """

    id: str
    title: str
    level: Level
    detail: str
    consequence: str = ""
    fix: str = ""


@dataclass(frozen=True)
class Claim:
    """One thing a report could assert, and whether this trace can back it.

    ``supported`` is deliberately tri-state. ``None`` means the trace cannot
    settle it — the doctor's own honesty rule applied to itself. Forcing that
    into ``False`` told a correctly instrumented router that no node carried a
    judgeable payload; forcing it into ``True`` is the failure this command
    exists to prevent.
    """

    name: str
    supported: bool | None
    reason: str


@dataclass(frozen=True)
class GraphDiagnosis:
    graph_id: str
    graph_type: str | None
    run_count: int
    edge_count: int
    checks: list[Check]
    claims: list[Claim]


@dataclass
class Diagnosis:
    """Everything ``doctor`` learned about one trace file."""

    source: str
    span_count: int
    agent_span_count: int
    span_kinds: dict[str, int] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    graphs: list[GraphDiagnosis] = field(default_factory=list)

    @property
    def run_count(self) -> int:
        return sum(g.run_count for g in self.graphs)

    @property
    def worst_level(self) -> Level:
        levels = [c.level for c in self.checks]
        levels += [c.level for g in self.graphs for c in g.checks]
        return max(levels, key=lambda lv: _LEVEL_RANK[lv], default="ok")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Status records — the failure that cost a session
# ---------------------------------------------------------------------------

# THREE tiers, not one, because a keyword list cannot separate a progress ping
# from terse real work and pretending otherwise produced two symmetric lies:
# `{"action": "escalate_to_legal"}` from a working router was reported as a
# broken payload, and a deliverable reading `"ok"` was reported as artifact
# text. Anything the vocabulary cannot settle is reported as UNCLEAR — an
# explicit "the doctor cannot tell" is the only honest third answer, and it is
# the answer this whole product is built on.

# Tier 1 — words about the RUN, never about the work. A value under one of
# these is a state word by construction: "step": "collect files" is still a step.
_LIFECYCLE_KEYS = frozenset(
    {
        "ok", "okay", "success", "succeeded", "successful", "status", "state",
        "step", "stage", "phase", "done", "complete", "completed", "finished",
        "error", "errors", "failed", "failure", "running", "pending", "skipped",
    }
)

# Tier 2 — counters and timers that ride along beside a state word.
_TELEMETRY_KEYS = frozenset(
    {
        "count", "n", "total", "index", "i", "iteration", "iter", "attempt",
        "attempts", "retries", "retry", "progress", "percent", "elapsed",
        "elapsed_ms", "duration", "duration_ms", "latency_ms", "ts",
        "timestamp", "time", "started", "ended", "start", "end",
    }
)

# Tier 3 — the keys the old single list got wrong. A router puts its decision
# in "action", a classifier its label in "result"/"code", a QA agent its answer
# in "message". Each of those is also what a ping uses. Short values under
# these keys are UNCLEAR: the doctor says it cannot tell instead of guessing.
_AMBIGUOUS_KEYS = frozenset(
    {
        "result", "results", "message", "msg", "action", "event", "node",
        "code", "output", "answer", "response", "reply", "decision", "label",
        "category", "class", "verdict", "reason", "summary", "value", "data",
        "text", "content", "type", "kind", "name", "id",
    }
)

# A whole output that is one of these words reports THAT the step ran, never
# what it produced — the non-JSON encoding of the same failure. Without this a
# two-character deliverable reading "ok" earned an affirmative terminal check.
_BARE_STATUS_WORDS = frozenset(
    {
        "ok", "okay", "done", "success", "succeeded", "successful", "complete",
        "completed", "finished", "failed", "failure", "error", "true", "false",
        "null", "none", "noop", "pass", "passed", "fail", "n/a", "na",
    }
)

# Payloads that encode "nothing" as literal JSON rather than as an empty string.
_EMPTY_LITERALS = frozenset({"{}", "[]", "null", '""', "''"})

# A status ping is small by nature; anything larger is presumed to be work
# even if it happens to use these key names.
_STATUS_MAX_CHARS = 600
# What makes a string read as content rather than as a token: a sentence's
# worth of words, or simple bulk. "escalate_to_legal" is one token; "some
# output text" is three and a judge has something to read.
_CONTENT_MIN_WORDS = 3
_CONTENT_MIN_CHARS = 60

PayloadKind = Literal["empty", "ping", "unclear", "work"]


def _carries_content(value: Any) -> bool:
    """Does this value read as content rather than as a token, flag or count?"""
    if isinstance(value, str):
        stripped = value.strip()
        return len(stripped) >= _CONTENT_MIN_CHARS or len(stripped.split()) >= _CONTENT_MIN_WORDS
    if isinstance(value, dict):
        return any(_carries_content(v) for v in value.values())
    if isinstance(value, list):
        return any(_carries_content(v) for v in value)
    return False


def _classify_object(obj: dict[str, Any]) -> PayloadKind:
    lifecycle = 0
    ambiguous = 0
    # Keys outside every vocabulary whose value is too short to be content: a
    # correlation id, a slug, a code. On their own they say the payload is
    # domain data ({"company": "Acme"}); riding beside a state word they are
    # metadata on a ping, and treating them as proof of work let ONE extra field
    # defeat the whole check — {"ok": true, "step": "plan", "run_id": "abc-123"}
    # earned a clean bill at the exact position (a chain's first node) the
    # command exists to inspect.
    domain_tokens = 0
    for key, value in obj.items():
        name = str(key).strip().strip("_").lower()
        if name in _LIFECYCLE_KEYS:
            lifecycle += 1
            # A lifecycle key's own value is a state word however wordy, so only
            # sheer bulk under one counts as content.
            if isinstance(value, str):
                if len(value.strip()) >= _CONTENT_MIN_CHARS:
                    return "work"
            elif _carries_content(value):
                return "work"
        elif name in _TELEMETRY_KEYS:
            if _carries_content(value):
                return "work"
        elif name in _AMBIGUOUS_KEYS:
            ambiguous += 1
            if _carries_content(value):
                return "work"
        elif isinstance(value, (bool, int, float)) or value is None:
            continue  # a bare flag or count riding along
        elif _carries_content(value):
            # Substantive text under a key no vocabulary claims: the work product.
            return "work"
        else:
            domain_tokens += 1
    if lifecycle and not ambiguous and not domain_tokens:
        return "ping"
    if domain_tokens and not lifecycle and not ambiguous:
        # Domain keys and no lifecycle vocabulary at all — nothing here suggests
        # a status report. {"company": "Acme"} is short, and it is still the work.
        return "work"
    # Lifecycle words AND opaque short fields, or ambiguous keys: genuinely
    # undecidable from the payload alone. Saying so is the point of this command.
    return "unclear"


def classify_payload(payload: str | None) -> PayloadKind:
    """What kind of thing is in ``output.value`` — and honestly, when unknown.

    ``ping``     it reports THAT the step ran: ``{"ok": true, "step":
                 "collect"}``, or a whole output reading ``"done"``.
    ``work``     it carries a work product a judge could read.
    ``unclear``  too short or too generic to tell the two apart:
                 ``{"action": "escalate_to_legal"}`` is a working router's
                 decision AND the shape of a ping. The doctor says so instead
                 of picking one — every caller must keep the distinction.
    ``empty``    nothing at all, including ``{}`` / ``null``.
    """
    text = (payload or "").strip()
    if not text or text in _EMPTY_LITERALS:
        return "empty"
    if len(text) > _STATUS_MAX_CHARS:
        return "work"
    if text.startswith("{") or text.startswith("["):
        try:
            obj = json.loads(text)
        except ValueError:
            # Truncated, or merely brace-prefixed prose. Fall through to the
            # text rule rather than guessing at a shape that did not parse.
            return "work" if _carries_content(text) else "unclear"
        if isinstance(obj, dict):
            return _classify_object(obj)
        if isinstance(obj, list):
            return "work" if any(_carries_content(v) for v in obj) else "unclear"
    if text.strip(".!;:,\"'`* \n\t").lower() in _BARE_STATUS_WORDS:
        return "ping"
    return "work" if _carries_content(text) else "unclear"


def status_record_keys(payload: str | None) -> list[str] | None:
    """The keys of ``payload`` when it is a JSON STATUS PING, else ``None``.

    Answers only for JSON objects — a bare-string ping has no keys to show, so
    ``classify_payload`` is the predicate and this is the display helper.
    """
    text = (payload or "").strip()
    if not text.startswith("{") or classify_payload(text) != "ping":
        return None
    try:
        obj = json.loads(text)
    except ValueError:  # pragma: no cover — classify_payload already parsed it
        return None
    return list(obj) if isinstance(obj, dict) else None


def payload_judgeable(payload: str | None) -> bool:
    """Could a per-node quality judge definitely learn something from this?

    ``False`` for an unclear payload too: "the doctor cannot tell" is not the
    same as "yes", and this predicate is the one that feeds affirmative claims.
    Callers that need the difference read ``classify_payload`` directly.
    """
    return classify_payload(payload) == "work"


def _payload_shape(payload: str | None) -> str:
    """How to show an output in a finding, without reprinting a whole document."""
    keys = status_record_keys(payload)
    if keys:
        return "{" + ", ".join(str(k) for k in keys[:4]) + "}"
    text = " ".join((payload or "").split())
    return f'"{text[:40]}…"' if len(text) > 40 else f'"{text}"'


# ---------------------------------------------------------------------------
# Span census
# ---------------------------------------------------------------------------


def _attribute(span: dict[str, Any], key: str) -> Any:
    """One attribute out of either accepted span encoding.

    ``map_spans`` returns only what became a run, so it cannot answer the
    doctor's first question — how many spans did NOT become runs, and why. A
    CHAIN span is invisible to the mapper by design; here it is the finding.
    """
    attrs = span.get("attributes")
    value: Any = None
    if isinstance(attrs, dict):
        value = attrs.get(key)
    elif isinstance(attrs, list):
        for item in attrs:
            if isinstance(item, dict) and item.get("key") == key:
                value = item.get("value")
                break
    if isinstance(value, dict):
        # OTLP AnyValue wrapper; a span kind is always the string form.
        value = value.get("stringValue")
    return value


def span_kind_census(spans: list[dict[str, Any]]) -> Counter[str]:
    """How many spans carry each ``openinference.span.kind``.

    Spans carrying none are counted under ``no-kind`` — they are the majority
    in a trace from an app that never adopted the convention, and lumping them
    in with a real kind would hide exactly that.
    """
    counts: Counter[str] = Counter()
    for span in spans:
        kind = _attribute(span, AGENT_KIND_ATTRIBUTE)
        label = str(kind).strip().upper() if isinstance(kind, str) and kind.strip() else "no-kind"
        counts[label] += 1
    return counts


def _census_phrase(counts: Counter[str]) -> str:
    return ", ".join(f"{kind} {n}" for kind, n in counts.most_common())


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _label(run: RunRecord) -> str:
    return run.agent_name or str(run.run_id)[:8]


def _labels(runs: list[RunRecord]) -> str:
    """Comma-separated names, disambiguated when two runs share one.

    Two runs of the same agent are a real shape (a retried step, a fan-out), and
    printing "scraper, scraper" reads as a rendering bug rather than as the two
    distinct sinks it is.
    """
    counts = Counter(_label(r) for r in runs)
    names = [
        _label(r) if counts[_label(r)] == 1 else f"{_label(r)}#{str(r.run_id)[:4]}"
        for r in runs
    ]
    return ", ".join(sorted(names))


def _check_agent_spans(span_count: int, counts: Counter[str]) -> Check:
    agent = counts.get("AGENT", 0)
    if span_count == 0:
        return Check(
            id="agent_spans",
            title="AGENT spans",
            level="gap",
            detail="the file carries no spans at all",
            consequence="there is nothing to analyse",
            fix="check the exporter actually flushed before the process exited",
        )
    if agent == 0:
        return Check(
            id="agent_spans",
            title="AGENT spans",
            level="gap",
            detail=(
                f"0 of {span_count} spans carry {AGENT_KIND_ATTRIBUTE}=AGENT "
                f"(seen: {_census_phrase(counts)})"
            ),
            consequence=(
                "no span becomes a node, so there is no graph: no localization, "
                "no propagation, no verdict of any kind"
            ),
            fix=(
                "set openinference.span.kind=AGENT on the span wrapping each agent "
                "step. Framework auto-instrumentors mark nodes CHAIN — promote them "
                "with detective_sdk.otel.collect(promote=..., chain=True), or use "
                "detective_sdk.run()/step() directly"
            ),
        )
    return Check(
        id="agent_spans",
        title="AGENT spans",
        level="ok",
        detail=(
            f"{agent} of {span_count} spans carry {AGENT_KIND_ATTRIBUTE}=AGENT "
            f"({_census_phrase(counts)})"
        ),
    )


def _check_names(bundle: GraphBundle) -> Check:
    named = [r for r in bundle.runs if (r.agent_name or "").strip()]
    total = len(bundle.runs)
    if not named:
        return Check(
            id="agent_names",
            title="agent names",
            level="gap",
            detail=f"0 of {total} runs carry gen_ai.agent.name",
            consequence=(
                "nodes are reported as span-id fragments, and role detection never "
                "engages — a qa/eval node is graded as if it had produced the "
                "artifact instead of a verdict on it"
            ),
            fix="set gen_ai.agent.name on the AGENT span (or on the OTLP resource)",
        )
    if len(named) < total:
        return Check(
            id="agent_names",
            title="agent names",
            level="warn",
            detail=f"{len(named)} of {total} runs carry gen_ai.agent.name",
            consequence=(
                "the unnamed nodes print as span-id fragments and cannot be matched "
                "to a role or to historical baselines across runs"
            ),
            fix="set gen_ai.agent.name on every AGENT span",
        )
    return Check(
        id="agent_names",
        title="agent names",
        level="ok",
        detail=f"{total} of {total} runs carry gen_ai.agent.name",
    )


def _check_edges(bundle: GraphBundle) -> Check:
    runs, edges = len(bundle.runs), len(bundle.edges)
    if runs < 2:
        return Check(
            id="edges",
            title="edges",
            level="ok",
            detail="single-node graph — there is no handoff to reconstruct",
        )
    if edges == 0:
        return Check(
            id="edges",
            title="edges",
            level="gap",
            detail=f"0 edges between {runs} runs",
            consequence=(
                "the runs are an unordered bag: blame has no path to walk, so no "
                "cut point, no drop between neighbours, no propagation into the "
                "deliverable and no shadowing"
            ),
            fix=(
                "nest each agent's span under the span of whoever called it (SPAWN), "
                "or set gen_ai.tool.target_agent on the delegating TOOL span. "
                "detective_sdk's run().step() chains steps for you"
            ),
        )
    return Check(
        id="edges",
        title="edges",
        level="ok",
        detail=f"{edges} edge(s) across {runs} runs",
    )


def _wrapper_roots(bundle: GraphBundle) -> set[Any]:
    """Runs that only wrap other runs: no incoming edge, an outgoing edge, AND
    no output of their own.

    The output test is the entire point. The previous version was purely
    topological, so it excluded the first node of ANY linear chain — on this
    repo's own ``testdata/demo_pipeline_happy.json`` that is ``orchestrator``,
    which carries 162 chars of output and ``cost_usd=0.012``, the largest single
    cost in the graph. Dropping it left the doctor certifying `ok cost 5/5` over
    a population the analysis prices across six runs, and reporting `ok payload
    content` on a chain whose first node shipped ``{"ok": true, "step": "plan"}``.
    An exclusion that never looks at the output cannot claim the output is empty.
    """
    incoming = {e.to_run_id for e in bundle.edges}
    outgoing = {e.from_run_id for e in bundle.edges}
    return {
        r.run_id
        for r in bundle.runs
        if r.run_id not in incoming
        and r.run_id in outgoing
        and not (r.output_inline or "").strip()
    }


def _check_topology(bundle: GraphBundle) -> Check:
    outgoing = {e.from_run_id for e in bundle.edges}
    sinks = [r for r in bundle.runs if r.run_id not in outgoing]
    deliverable = deliverable_run(bundle)
    where = f"deliverable resolves to `{_label(deliverable)}`" if deliverable else "no run"
    if not sinks:
        return Check(
            id="topology",
            title="sink node",
            level="warn",
            detail=f"every run has an outgoing edge (a cycle, e.g. a retry loop) — {where}",
            consequence=(
                "there is no true sink, so the run whose output represents the graph "
                "is inferred from end times rather than read from the topology"
            ),
            fix=(
                "keep the retry edge but let the final producer end the chain, or "
                "emit the deliverable from a node nothing feeds back into"
            ),
        )
    if len(sinks) > 1:
        names = _labels(sinks)
        return Check(
            id="topology",
            title="sink node",
            level="warn",
            detail=f"{len(sinks)} runs have no outgoing edge ({names}) — {where}",
            consequence=(
                "the graph has no single deliverable: the latest-ended sink is graded "
                "and the others are never checked against the request"
            ),
            fix="connect the parallel branches into the node that assembles the result",
        )
    return Check(
        id="topology",
        title="sink node",
        level="ok",
        detail=f"one sink (`{_label(sinks[0])}`) — {where}",
    )


def _wrapper_labels(bundle: GraphBundle) -> str:
    """The names of the wrapper roots, or ``""`` when there are none."""
    wrappers = _wrapper_roots(bundle)
    if not wrappers or len(wrappers) == len(bundle.runs):
        return ""
    return _labels([r for r in bundle.runs if r.run_id in wrappers])


def _producing_runs(bundle: GraphBundle) -> tuple[list[RunRecord], str]:
    """The runs whose OUTPUT is graded, and the disclosure that goes with it.

    Only the payload-shaped checks use this, and only wrapper roots are left
    out: a root span with an empty output is the one run whose missing payload
    is not a finding. Cost and model deliberately keep the full population — a
    span that spent money spent it whether or not it emitted an output, and the
    analysis prices every run in the bundle.

    Whatever is left out is NAMED, together with what the analysis will still
    say about it. A denominator that silently shrinks is how the old version
    printed `ok` over runs it had never looked at.
    """
    wrappers = _wrapper_roots(bundle)
    producing = [r for r in bundle.runs if r.run_id not in wrappers] or list(bundle.runs)
    names = _wrapper_labels(bundle)
    if not names or len(producing) == len(bundle.runs):
        return producing, ""
    plural = len(bundle.runs) - len(producing) > 1
    note = (
        f" · not graded here: {names} (root span{'s' if plural else ''} with no output "
        f"of {'their' if plural else 'its'} own — the analysis still reports "
        f"{'them' if plural else 'it'} unscored)"
    )
    return producing, note


def _check_payloads(bundle: GraphBundle) -> Check:
    graded, note = _producing_runs(bundle)
    missing_out = [r for r in graded if not (r.output_inline or "").strip()]
    missing_in = [r for r in graded if not (r.input_inline or "").strip()]

    if graded and len(missing_out) == len(graded):
        return Check(
            id="payloads",
            title="payloads",
            level="gap",
            detail=f"no run carries a non-empty output.value{note}",
            consequence=(
                "every node is reported unscored and the deliverable cannot be "
                "checked against the request — the analysis observes nothing"
            ),
            fix="record input.value and output.value on each AGENT span",
        )
    if missing_out or missing_in:
        # Deduped by run, not by name: a run missing BOTH payloads is one node.
        lost = _labels(list({r.run_id: r for r in missing_out + missing_in}.values()))
        return Check(
            id="payloads",
            title="payloads",
            level="warn",
            detail=(
                f"{len(graded) - len(missing_out)}/{len(graded)} runs have an output, "
                f"{len(graded) - len(missing_in)}/{len(graded)} an input — "
                f"thin at: {lost}{note}"
            ),
            consequence=(
                "those nodes stay unscored: with no output there is nothing to judge, "
                "and with no input a bad result cannot be separated from a bad handoff"
            ),
            fix="record input.value and output.value on every AGENT span",
        )
    return Check(
        id="payloads",
        title="payloads",
        level="ok",
        detail=f"{len(graded)}/{len(graded)} runs carry input.value and output.value{note}",
    )


def _check_status_records(bundle: GraphBundle) -> Check:
    graded, note = _producing_runs(bundle)
    kinds = {r.run_id: classify_payload(r.output_inline) for r in graded}
    pings = [r for r in graded if kinds[r.run_id] == "ping"]
    unclear = [r for r in graded if kinds[r.run_id] == "unclear"]
    readable = [r for r in graded if kinds[r.run_id] != "empty"]

    def listing(runs: list[RunRecord]) -> str:
        return "; ".join(
            f"`{_label(r)}` → {_payload_shape(r.output_inline)}" for r in runs[:3]
        )

    if pings:
        tail = (
            f" · {len(unclear)} further output(s) are too short or too generic to tell "
            "apart from a ping"
            if unclear
            else ""
        )
        return Check(
            id="status_records",
            title="payload content",
            level="warn",
            detail=(
                f"{len(pings)} of {len(readable)} outputs are STATUS RECORDS, not work: "
                f"{listing(pings)}{tail}{note}"
            ),
            consequence=(
                "per-node quality cannot be judged from this — a judge handed "
                '{"ok": true, "step": ...} grades the progress ping, not the step, and '
                "the report stays confident about work it never saw"
            ),
            fix=(
                "put the step's actual product in output.value (the document, the rows, "
                "the answer). Keep the status object in a separate attribute if you need it"
            ),
        )
    if unclear:
        # NOT "these are pings". A router's {"action": "escalate_to_legal"} and a
        # progress ping are the same shape, and the previous version called both
        # broken instrumentation. The doctor states the ambiguity and hands the
        # reader the one question it cannot answer for them.
        return Check(
            id="status_records",
            title="payload content",
            level="warn",
            detail=(
                f"{len(unclear)} of {len(readable)} outputs are too short or too generic "
                f"for the doctor to tell whether they carry work: {listing(unclear)}{note}"
            ),
            consequence=(
                "whether these nodes are judgeable is unknown from the trace alone: if "
                "these strings are the step's product the analysis is fine, and if they "
                "are progress pings every score describing them is a score of a progress "
                "bar — nothing downstream reports which"
            ),
            fix=(
                "read the listed output.value. If it already is the step's product, "
                "nothing to change; if the product lives elsewhere, move it into "
                "output.value and keep the short form in a separate attribute"
            ),
        )
    if not readable:
        # Deliberately not an affirmative: there was nothing to look at. The
        # payloads check above already carries the finding.
        return Check(
            id="status_records",
            title="payload content",
            level="ok",
            detail="no output to read — see the payloads check",
        )
    return Check(
        id="status_records",
        title="payload content",
        level="ok",
        detail=(
            f"{len(readable)}/{len(readable)} outputs carry enough text to read as work "
            "rather than as a status ping"
        ),
    )


def _check_cost(bundle: GraphBundle) -> Check:
    # The full population on purpose: a span that spent money spent it whether
    # or not it emitted an output, and `bundle.total_cost_usd` sums every run.
    # The old version excluded roots here and then printed `ok cost 5/5` on a
    # trace whose excluded root was 32% of the priced total.
    graded = list(bundle.runs)
    total = len(graded)
    costed = [r for r in graded if r.cost_usd is not None]
    tokened = [r for r in graded if r.tokens_in is not None or r.tokens_out is not None]
    have = f"gen_ai.usage.cost {len(costed)}/{total}, tokens {len(tokened)}/{total}"
    uncosted = _labels([r for r in graded if r.cost_usd is None])
    zero_fix = (
        "set gen_ai.usage.cost on every AGENT span — on a wrapper span that spends "
        "nothing, an explicit 0 says so, where an absent attribute only says unknown"
    )
    if not costed and not tokened:
        return Check(
            id="cost",
            title="cost / tokens",
            level="warn",
            detail=f"no run carries a gen_ai.usage.* attribute (0/{total})",
            consequence=(
                "cost stays unknown — the downstream cost of a fault is reported as "
                "unknown, never as $0, so a rerun cannot be priced"
            ),
            fix=(
                "set gen_ai.usage.cost and gen_ai.usage.input_tokens / "
                "gen_ai.usage.output_tokens on the AGENT span or its LLM children "
                "(detective_sdk: span.cost(usd=..., tokens_in=..., tokens_out=...))"
            ),
        )
    if not costed:
        return Check(
            id="cost",
            title="cost / tokens",
            level="warn",
            detail=f"tokens but no gen_ai.usage.cost ({have})",
            consequence=(
                "cost stays unknown: no pricing table ships with the analysis, so "
                "tokens are never converted into money"
            ),
            fix="set gen_ai.usage.cost alongside the token counts",
        )
    if len(costed) < total:
        return Check(
            id="cost",
            title="cost / tokens",
            level="warn",
            detail=f"partial cost coverage ({have}) — no cost on: {uncosted}",
            consequence=(
                "the downstream cost of a fault is a lower bound only — the uncosted "
                "nodes contribute nothing rather than an estimate"
            ),
            fix=zero_fix,
        )
    return Check(
        id="cost",
        title="cost / tokens",
        level="ok",
        detail=have,
    )


def _check_model(bundle: GraphBundle, models: dict[Any, str | None]) -> Check:
    # Full population, same reason as cost: model attribution is a property of
    # every run the analysis reports on, not only of the ones with an output.
    graded = list(bundle.runs)
    total = len(graded)
    modelled = [r for r in graded if models.get(r.run_id)]
    unattributed = _labels([r for r in graded if not models.get(r.run_id)])
    seen = sorted({str(models[r.run_id]) for r in modelled})
    if not modelled:
        return Check(
            id="model",
            title="model",
            level="warn",
            detail=f"gen_ai.request.model on 0/{total} runs",
            consequence=(
                "which model produced each node is unknown, so a quality change "
                "after a model swap cannot be attributed to the swap — scores from "
                "two different models are compared as if they came from one node"
            ),
            fix=(
                "set gen_ai.request.model on the AGENT span, or let the LLM child "
                "span carry it (the mapper reads it off children too)"
            ),
        )
    if len(modelled) < total:
        return Check(
            id="model",
            title="model",
            level="warn",
            detail=(
                f"gen_ai.request.model on {len(modelled)}/{total} runs "
                f"({', '.join(seen)}) — no model on: {unattributed}"
            ),
            consequence=(
                "the unattributed nodes cannot be compared across model versions"
            ),
            fix="set gen_ai.request.model on every AGENT span or its LLM child",
        )
    return Check(
        id="model",
        title="model",
        level="ok",
        detail=f"{total}/{total} runs name a model ({', '.join(seen)})",
    )


def _check_deliverable(bundle: GraphBundle) -> Check:
    run = deliverable_run(bundle)
    if run is None:
        return Check(
            id="deliverable",
            title="deliverable text",
            level="gap",
            detail="no run to grade",
            consequence="the terminal check cannot run",
            fix="record at least one producing AGENT span",
        )
    name = _label(run)
    output = run.output_inline or ""
    if not output.strip():
        return Check(
            id="deliverable",
            title="deliverable text",
            level="gap",
            detail=f"`{name}` has an empty output.value",
            consequence=(
                "the terminal check is not_checkable: the request cannot be confirmed "
                "OR denied, and no propagation into the shipped artifact can be shown"
            ),
            fix="record the produced artifact in output.value on the deliverable span",
        )
    refs = opaque_artifact_refs(output)
    if refs:
        return Check(
            id="deliverable",
            title="deliverable text",
            level="warn",
            detail=f"`{name}` references {', '.join(refs[:3])} but embeds no artifact text",
            consequence=(
                "the terminal check grades a description of the file, not the file — "
                "'meets all requirements' would be an assertion about an unopened "
                "artifact, so the verdict is downgraded to not_checkable"
            ),
            fix=(
                "append the extracted text as an [artifact_text <path>]: block "
                "(detective_sdk.artifact_meta_block) so the content travels with the trace"
            ),
        )
    kind = classify_payload(output)
    if kind == "ping":
        return Check(
            id="deliverable",
            title="deliverable text",
            level="warn",
            detail=(
                f"`{name}` ships a status record instead of the artifact: "
                f"{_payload_shape(output)}"
            ),
            consequence=(
                "the terminal check grades a progress ping: whether the run met the "
                "request is unknowable from this trace"
            ),
            fix="put the produced artifact in output.value on the deliverable span",
        )
    if kind == "unclear":
        # Not "this is a ping". A one-line answer from a QA agent and a status
        # line look identical from here; saying which would be a guess dressed
        # as a finding, and the previous version guessed both ways.
        return Check(
            id="deliverable",
            title="deliverable text",
            level="warn",
            detail=(
                f"`{name}` carries {len(output.strip())} chars the doctor cannot tell "
                f"apart from a status line: {_payload_shape(output)}"
            ),
            consequence=(
                "whether the terminal check would grade the artifact or a status line "
                "is unknown from this trace — if this string is the deliverable the "
                "check holds, and nothing downstream reports which of the two it read"
            ),
            fix=(
                "if the artifact lives elsewhere, embed its text as an "
                "[artifact_text <path>]: block (detective_sdk.artifact_meta_block); "
                "if this string really is the deliverable, nothing to change"
            ),
        )
    return Check(
        id="deliverable",
        title="deliverable text",
        level="ok",
        detail=f"`{name}` carries {len(output)} chars of artifact text",
    )


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


def _claims(bundle: GraphBundle, checks: dict[str, Check]) -> list[Claim]:
    """The three headline claims a report makes, answered for THIS trace.

    ``docs/trace-requirements.md`` states the dependencies as prose — "content
    localization needs per-node payloads (judgeable) + node ordering/edges",
    "breach propagated needs the deliverable producer's payload". This turns
    each row into an answer about the file in front of the reader, which is the
    whole reason the doctor exists rather than a link to the doc.

    A claim can also come back ``None`` — "this trace cannot settle it". That is
    not hedging: a router whose nodes emit ``{"action": "escalate_to_legal"}``
    was previously told outright that no node carried a judgeable payload, which
    is a false negative in the same family as the false `ok`s it replaced.
    """
    graded, _ = _producing_runs(bundle)
    kinds = {r.run_id: classify_payload(r.output_inline) for r in graded}
    work = [r for r in graded if kinds[r.run_id] == "work"]
    unclear = [r for r in graded if kinds[r.run_id] == "unclear"]
    blocked = [r for r in graded if kinds[r.run_id] in ("ping", "empty")]
    total = len(graded)
    wrappers = _wrapper_labels(bundle)
    many = len(bundle.runs) - total > 1
    aside = (
        f" ({wrappers} produce{'' if many else 's'} nothing of "
        f"{'their' if many else 'its'} own and report{'' if many else 's'} unscored)"
        if wrappers
        else ""
    )

    if len(bundle.runs) < 2:
        # The doctor's own edges consequence says 0 edges means blame has no
        # path to walk; claiming "localization: yes, 1/1 across 0 edge(s)" in
        # the next block contradicted it two lines later.
        localization = Claim(
            "localization",
            False,
            "single-node graph — with no handoff there is no drop between neighbours "
            "to locate; the one node is the whole graph",
        )
    elif not bundle.edges:
        localization = Claim(
            "localization",
            False,
            f"{len(work)}/{total} nodes judgeable but 0 edges — a drop "
            "between neighbours cannot be located",
        )
    elif not work and not unclear:
        localization = Claim(
            "localization",
            False,
            "no node carries a judgeable payload — every node reports unscored",
        )
    elif blocked:
        also = (
            f", {len(unclear)} more too short or too generic to tell" if unclear else ""
        )
        localization = Claim(
            "localization",
            False,
            f"only {len(work)}/{total} nodes carry a judgeable payload{also} — the "
            "unjudged ones are an observability boundary the origin can hide behind",
        )
    elif unclear:
        localization = Claim(
            "localization",
            None,
            f"{len(work)}/{total} nodes read as work and {len(unclear)} are too short "
            "or too generic for the doctor to tell — whether every node is judgeable "
            "cannot be established from this trace",
        )
    else:
        localization = Claim(
            "localization",
            True,
            f"{total}/{total} nodes judgeable across {len(bundle.edges)} edge(s){aside}",
        )

    # Read off the numbers, not off the check's level: a missing model name is
    # a warning that has nothing to do with whether cost can be claimed. The
    # population is every run in the bundle, because that is what the analysis
    # prices — a shrunken denominator is how `yes cost` was asserted over a
    # trace with a real uncosted node in it.
    priced = list(bundle.runs)
    costed = [r for r in priced if r.cost_usd is not None]
    if costed and len(costed) == len(priced):
        cost = Claim("cost", True, f"gen_ai.usage.cost on {len(costed)}/{len(priced)} runs")
    elif costed:
        cost = Claim(
            "cost",
            False,
            f"only {len(costed)}/{len(priced)} runs are costed — any total is a "
            "lower bound, never the price of the run",
        )
    else:
        cost = Claim("cost", False, "no run carries gen_ai.usage.cost — cost stays unknown")

    deliverable_check = checks["deliverable"]
    deliverable = deliverable_run(bundle)
    deliverable_kind = (
        classify_payload(deliverable.output_inline) if deliverable is not None else "empty"
    )
    if deliverable_check.level == "ok":
        terminal = Claim("terminal check", True, deliverable_check.detail)
    elif deliverable_kind == "unclear":
        terminal = Claim(
            "terminal check",
            None,
            "the deliverable's output is too short or too generic for the doctor to "
            "tell whether it is the artifact or a status line",
        )
    else:
        terminal = Claim(
            "terminal check",
            False,
            "the deliverable's content is not visible in the trace",
        )
    return [localization, cost, terminal]


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


def diagnose(
    exports: list[dict[str, Any]], source: str, *, a2a_detection: bool = False
) -> Diagnosis:
    """Read a trace the way the analysis will, and report what it can support."""
    spans = [span for export in exports for span in flatten_export_request(export)]
    counts = span_kind_census(spans)
    result = map_spans(spans, a2a_detection=a2a_detection)
    bundles = bundles_from_mapping(result)
    # model_name never reaches a RunRecord (the deployed schema has no column
    # for it), so it is read back off the mapper's candidates.
    models = {run_id_from_key(c.run_key): c.model_name for c in result.runs}

    diagnosis = Diagnosis(
        source=source,
        span_count=len(spans),
        agent_span_count=counts.get("AGENT", 0),
        span_kinds=dict(counts),
        checks=[_check_agent_spans(len(spans), counts)],
    )
    for bundle in bundles:
        checks = [
            _check_names(bundle),
            _check_edges(bundle),
            _check_topology(bundle),
            _check_payloads(bundle),
            _check_status_records(bundle),
            _check_cost(bundle),
            _check_model(bundle, models),
            _check_deliverable(bundle),
        ]
        by_id = {c.id: c for c in checks}
        diagnosis.graphs.append(
            GraphDiagnosis(
                graph_id=str(bundle.graph_id),
                graph_type=bundle.graph_type,
                run_count=len(bundle.runs),
                edge_count=len(bundle.edges),
                checks=checks,
                claims=_claims(bundle, by_id),
            )
        )
    return diagnosis


def unreadable_diagnosis(source: str, reason: str) -> Diagnosis:
    """A file the loader rejected is itself the first instrumentation finding.

    Reported rather than raised: "your exporter wrote protobuf" is exactly the
    kind of thing a pre-flight check exists to say out loud.
    """
    return Diagnosis(
        source=source,
        span_count=0,
        agent_span_count=0,
        checks=[
            Check(
                id="trace_format",
                title="trace format",
                level="gap",
                detail=reason,
                consequence="nothing can be read from this file",
                fix=(
                    "export the OTLP/HTTP JSON encoding of ExportTraceServiceRequest "
                    "(OTEL_EXPORTER_OTLP_PROTOCOL=http/json); protobuf is not accepted"
                ),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Sentences here are long by design (a consequence has to be a full thought),
# so they are soft-wrapped to a total width that survives an 80-column terminal.
_WRAP = 88
# ...unless the label column is wide, in which case the text column keeps a
# floor rather than degenerating into one word per line.
_MIN_BODY = 36


def _wrap(text: str, width: int = _WRAP) -> list[str]:
    """Soft-wrap on whitespace. No hyphenation, no dependency, no reflow of
    anything already short enough."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def render_doctor_terminal(diagnosis: Diagnosis, *, color: bool = True) -> str:
    paint = Painter(color)
    out: list[str] = []
    add = out.append

    add(paint(f"Agent Detective doctor — {diagnosis.source}", "bold"))
    add(
        paint(
            f"{diagnosis.span_count} span(s) · {diagnosis.agent_span_count} AGENT span(s) · "
            f"{len(diagnosis.graphs)} graph(s) · {diagnosis.run_count} agent run(s)",
            "dim",
        )
    )
    for line in _wrap(
        "A pre-flight check on the trace, not a verdict: what an analysis could "
        "claim from it, and what it never can."
    ):
        add(paint(line, "dim"))

    def emit_checks(checks: list[Check]) -> None:
        width = max((len(c.title) for c in checks), default=0)
        # 5 indent + 4 level + 1 space + title + 2 — every continuation line
        # lands under the detail column, so a wrapped sentence still reads as
        # belonging to its check.
        pad = " " * (5 + 4 + 1 + width + 2)
        body = max(_MIN_BODY, _WRAP - len(pad))
        for check in checks:
            tone = _LEVEL_TONE[check.level]
            detail = _wrap(check.detail, body)
            add(
                f"     {paint(check.level.ljust(4), tone)} "
                f"{paint(check.title.ljust(width), 'bold')}  {detail[0]}"
            )
            for line in detail[1:]:
                add(pad + line)
            if check.consequence:
                for line in _wrap(f"→ {check.consequence}", body):
                    add(paint(pad + line, "dim"))
            if check.fix:
                for line in _wrap(f"fix: {check.fix}", body):
                    add(paint(pad + line, "dim"))

    if diagnosis.checks:
        add("")
        add(paint("   Trace", "bold"))
        emit_checks(diagnosis.checks)

    for graph in diagnosis.graphs:
        add("")
        title = f"── graph {graph.graph_id[:8]}"
        if graph.graph_type:
            title += f"  [{graph.graph_type}]"
        add(paint(title, "bold"))
        add("")
        add(paint("   Capture", "bold"))
        emit_checks(graph.checks)

        add("")
        add(paint("   What you can claim", "bold"))
        width = max(len(c.name) for c in graph.claims)
        pad = " " * (5 + 3 + 2 + width + 2)
        body = max(_MIN_BODY, _WRAP - len(pad))
        for claim in graph.claims:
            # "?" is a first-class answer here, not a rendering fallback: see
            # Claim.supported.
            mark = {True: "yes", False: "no ", None: "?  "}[claim.supported]
            tone = {True: "ok", False: "fail", None: "unknown"}[claim.supported]
            reason = _wrap(claim.reason, body)
            add(
                f"     {paint(mark, tone)}  {claim.name.ljust(width)}  "
                f"{paint(reason[0], 'dim')}"
            )
            for line in reason[1:]:
                add(paint(pad + line, "dim"))

    if not diagnosis.graphs:
        add("")
        for line in _wrap(
            "No graph was reconstructed from this trace, so there is nothing an "
            "analysis could claim about it."
        ):
            add(paint(line, "bold", "fail"))

    add("")
    gaps = sum(
        1
        for c in [*diagnosis.checks, *(c for g in diagnosis.graphs for c in g.checks)]
        if c.level == "gap"
    )
    warns = sum(
        1
        for c in [*diagnosis.checks, *(c for g in diagnosis.graphs for c in g.checks)]
        if c.level == "warn"
    )
    if gaps or warns:
        add(paint(f"{gaps} gap(s), {warns} warning(s)", "bold", _LEVEL_TONE[diagnosis.worst_level]))
    else:
        add(paint("instrumentation carries everything the analysis reads", "bold", "ok"))
    add(paint("doctor never gates: this command always exits 0.", "dim"))
    return "\n".join(out)


def render_doctor_json(diagnosis: Diagnosis) -> dict[str, Any]:
    """The same diagnosis as data, so a setup script can act on it."""
    return {
        "source": diagnosis.source,
        "spans": diagnosis.span_count,
        "agent_spans": diagnosis.agent_span_count,
        "span_kinds": diagnosis.span_kinds,
        "worst_level": diagnosis.worst_level,
        "checks": [_check_json(c) for c in diagnosis.checks],
        "graphs": [
            {
                "graph_id": g.graph_id,
                "graph_type": g.graph_type,
                "run_count": g.run_count,
                "edge_count": g.edge_count,
                "checks": [_check_json(c) for c in g.checks],
                "claims": [
                    {"name": c.name, "supported": c.supported, "reason": c.reason}
                    for c in g.claims
                ],
            }
            for g in diagnosis.graphs
        ],
    }


def _check_json(check: Check) -> dict[str, Any]:
    return {
        "id": check.id,
        "title": check.title,
        "level": check.level,
        "detail": check.detail,
        "consequence": check.consequence,
        "fix": check.fix,
    }
