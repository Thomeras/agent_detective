"""Render an analysis for a terminal (and as Markdown).

Two rules shape everything here.

**Nothing is invented.** Every line traces to a typed field of the verdict —
defect kinds, origins, confidences, caveats, signals. Where a number is absent
the report prints ``—`` rather than a plausible default; an unscored node says
so, because presenting "no verdict" as a passing score is the exact failure
this project exists to catch.

**The reader's first question is answered first.** Did it pass; if not, where
did it break, what kind of fault, and how sure are we — in that order, before
any per-node detail.
"""

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

from .analyze import AnalysisRun, GraphAnalysis
from .descriptor import (
    CAVEAT_LABELS,
    Descriptor,
    NOT_VERIFIED_VERDICT,
    PASSED_VERDICT,
    UNANALYSED_VERDICT,
    culprit_heading,
    defect_descriptor,
    origin_phrase,
    verdict_descriptor,
)

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "ok": "\033[32m",
    "warn": "\033[33m",
    "fail": "\033[31m",
    "unknown": "\033[36m",
}


def color_enabled(stream: TextIO, *, force: bool | None = None) -> bool:
    """Honour NO_COLOR and non-TTY output; ``force`` overrides both."""
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class Painter:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(_ANSI.get(s, "") for s in styles)
        return f"{prefix}{text}{_ANSI['reset']}" if prefix else text


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)}%"


def _score(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _short(run_id: Any) -> str:
    return str(run_id)[:8]


class _GraphView:
    """The one place that turns a stored report into display-ready pieces.

    Both renderers read from here, so the terminal and the Markdown brief can
    never disagree about what the verdict was.
    """

    def __init__(self, analysis: GraphAnalysis) -> None:
        self.analysis = analysis
        self.report: dict[str, Any] = analysis.blame_report or {}
        self.evidence: dict[str, Any] = self.report.get("evidence") or {}
        self.names = analysis.agent_names

    def label(self, run_id: Any) -> str:
        return self.names.get(str(run_id), _short(run_id))

    def subject_label(self, subject: Any) -> str:
        """A finding's subject is ``run:<id>``, ``terminal`` or ``graph``.

        Only the first names a node; the other two are the graph-level subjects
        and stay as they are (``blame_engine.finding.run_subject``).
        """
        text = str(subject)
        if text.startswith("run:"):
            return self.label(text[len("run:") :])
        return text

    @property
    def report_type(self) -> str | None:
        return self.report.get("report_type")

    @property
    def measured(self) -> bool:
        """Did ANY channel actually produce a measurement for this graph?

        A scored node, a fired deterministic rule, or a terminal verdict the
        judge could genuinely check. If none of those exist, the analysis
        observed nothing — a state that must never be reported as a pass.
        """
        scores = (self.evidence.get("score_map") or {}).values()
        if any(s is not None for s in scores):
            return True
        if any(row.quality_score is not None for row in self.analysis.node_scores.values()):
            return True
        if self.evidence.get("deterministic_signals") or self.evidence.get(
            "contract_violations"
        ):
            return True
        verdict = self.analysis.verdict
        if verdict is not None and verdict.terminal_judge_verdict in ("ok", "bad"):
            return True
        return bool(verdict is not None and verdict.flags)

    @property
    def verdict(self) -> Descriptor:
        if self.analysis.verdict is None:
            return UNANALYSED_VERDICT
        if not self.measured:
            return NOT_VERIFIED_VERDICT
        if not self.report:
            # Tier2 wrote no report because tier1 raised no incident — the
            # cheap channel looked and found nothing wrong.
            return PASSED_VERDICT
        if self.analysis.incident is None and self.report_type in (None, "unclassified"):
            return PASSED_VERDICT
        return verdict_descriptor(self.report_type)

    @property
    def culprits(self) -> list[Any]:
        return list(self.report.get("culprit_run_ids") or [])

    @property
    def defects(self) -> list[dict[str, Any]]:
        return list(self.evidence.get("defects") or [])

    @property
    def findings(self) -> list[dict[str, Any]]:
        return list(self.evidence.get("findings") or [])

    def finding_line(self, ref: int) -> str | None:
        """One finding rendered as ``kind (basis)`` — its typed identity only."""
        if ref < 0 or ref >= len(self.findings):
            return None
        finding = self.findings[ref]
        kind = finding.get("kind", "finding")
        subject = finding.get("subject")
        where = self.subject_label(subject) if subject else None
        provenance = finding.get("provenance") or {}
        basis = provenance.get("label") or provenance.get("kind")
        certainty = finding.get("certainty")
        parts = [kind]
        if where:
            parts.append(f"at {where}")
        line = " ".join(parts)
        tail = []
        if basis:
            tail.append(str(basis))
        if isinstance(certainty, (int, float)):
            tail.append(f"certainty {_pct(float(certainty))}")
        return f"{line} ({', '.join(tail)})" if tail else line

    def caveats(self, defect: dict[str, Any]) -> list[str]:
        return [
            label
            for field, label in CAVEAT_LABELS.items()
            if defect.get(field)
        ]

    def node_rows(self) -> list[tuple[str, str, str]]:
        """(name, score, annotation) per run, in the engine's topological order."""
        score_map: dict[str, Any] = self.evidence.get("score_map") or {}
        order: list[str] = list(self.evidence.get("topo_order") or [])
        if not order:
            # No report (tier1-only, or nothing escalated): fall back to the
            # bundle's own run order, which the mapper already made deterministic.
            order = [str(run.run_id) for run in self.analysis.bundle.runs]

        culprits = {str(c) for c in self.culprits}
        manifestations = {str(m) for m in (self.evidence.get("manifestation_run_ids") or [])}
        verifiers = {str(v) for v in (self.evidence.get("verifier_run_ids") or [])}
        drops: dict[str, Any] = self.evidence.get("drops") or {}
        node_flags: dict[str, list[str]] = self.evidence.get("node_flags") or {}

        rows: list[tuple[str, str, str]] = []
        for run_id in order:
            score = score_map.get(run_id)
            if score is None and run_id in self.analysis.node_scores_by_str:
                score = self.analysis.node_scores_by_str[run_id].quality_score

            notes: list[str] = []
            if run_id in culprits:
                notes.append("ORIGIN")
            if run_id in manifestations:
                notes.append("surfaced here")
            if run_id in verifiers:
                notes.append("verifier")
            drop = drops.get(run_id)
            if isinstance(drop, (int, float)):
                notes.append(f"drop {drop:.2f}")
            if score is None:
                row = self.analysis.node_scores_by_str.get(run_id)
                reason = row.unscored_reason if row is not None else None
                notes.append(f"unscored ({reason})" if reason else "unscored")
            notes.extend(node_flags.get(run_id, []))
            rows.append((self.label(run_id), _score(score), ", ".join(notes)))
        return rows

    def signals(self) -> list[tuple[str, str, str, str]]:
        """(severity, name, node, detail) for every deterministic signal."""
        out: list[tuple[str, str, str, str]] = []
        for signal in self.evidence.get("deterministic_signals") or []:
            out.append(
                (
                    str(signal.get("severity", "")),
                    str(signal.get("name", "")),
                    self.label(signal.get("run_id")) if signal.get("run_id") else "graph",
                    str(signal.get("detail") or signal.get("basis") or ""),
                )
            )
        for violation in self.evidence.get("contract_violations") or []:
            out.append(
                (
                    "fail",
                    "contract_violation",
                    self.label(violation.get("run_id")),
                    f"{violation.get('key')}: {violation.get('from')} → {violation.get('to')}",
                )
            )
        return out

    def notes(self) -> list[str]:
        return [str(n) for n in (self.evidence.get("notes") or [])]


def unverified_graphs(run: AnalysisRun) -> list[GraphAnalysis]:
    """Graphs the analysis could not measure at all.

    Exposed because callers need to act on it: a CI gate that treats "nothing
    was measurable" as a pass has learned nothing from the run.
    """
    return [g for g in run.graphs if not _GraphView(g).measured]


def render_terminal(run: AnalysisRun, source: str, *, color: bool = True) -> str:
    paint = Painter(color)
    out: list[str] = []
    add = out.append

    add(paint(f"Agent Detective — {source}", "bold"))
    total_runs = sum(len(g.bundle.runs) for g in run.graphs)
    channel = (
        f"judged channel: on ({run.judge})"
        if run.judge_enabled
        else f"judged channel: OFF — {run.judge}"
    )
    add(
        paint(
            f"{len(run.graphs)} graph(s) · {total_runs} agent run(s) · {channel}",
            "dim",
        )
    )

    for graph in run.graphs:
        view = _GraphView(graph)
        verdict = view.verdict
        add("")
        title = f"── graph {_short(graph.graph_id)}"
        if graph.bundle.graph_type:
            title += f"  [{graph.bundle.graph_type}]"
        add(paint(title, "bold"))

        headline = paint(verdict.label, "bold", verdict.tone)
        bits = [headline]
        if view.report_type:
            bits.append(paint(view.report_type, "dim"))
        confidence = view.report.get("confidence")
        if isinstance(confidence, (int, float)):
            bits.append(paint(f"confidence {_pct(float(confidence))}", "dim"))
        add("   " + "  ·  ".join(bits))
        add("   " + verdict.template)

        if not graph.tier2_ran:
            add(
                paint(
                    "   tier1 found nothing worth deep analysis "
                    "(no per-node scoring ran)",
                    "dim",
                )
            )

        observation = view.evidence.get("observation_confidence")
        attribution = view.evidence.get("attribution_confidence")
        if observation is not None or attribution is not None:
            add(
                paint(
                    f"   observation {_pct(observation)} · attribution {_pct(attribution)}",
                    "dim",
                )
            )

        culprits = view.culprits
        if culprits:
            add("")
            add(paint("   " + culprit_heading(view.report_type, len(culprits) > 1), "bold"))
            for culprit in culprits:
                add(f"     {view.label(culprit)}")

        if view.defects:
            add("")
            add(paint("   Defects", "bold"))
            for defect in view.defects:
                origin = defect.get("origin") or {}
                desc = defect_descriptor(str(defect.get("kind")), origin)
                where = origin_phrase(origin, view.label)
                add(f"     {paint('●', desc.tone)} {paint(desc.label, 'bold')} — {where}")
                add(paint(f"       {desc.template.replace('{origin}', where)}", "dim"))
                add(
                    paint(
                        "       observation "
                        f"{_pct(defect.get('observation_confidence'))} · attribution "
                        f"{_pct(defect.get('attribution_confidence'))} · channel "
                        f"{defect.get('channel', '—')}",
                        "dim",
                    )
                )
                for ref in defect.get("finding_refs") or []:
                    line = view.finding_line(int(ref.get("ref", -1)))
                    if line:
                        add(paint(f"       {ref.get('role', 'context')}: {line}", "dim"))
                caveats = view.caveats(defect)
                if caveats:
                    add(paint(f"       caveats: {'; '.join(caveats)}", "dim"))

        rows = view.node_rows()
        if rows:
            add("")
            add(paint("   Pipeline", "bold"))
            width = max(len(name) for name, _, _ in rows)
            for name, score, note in rows:
                line = f"     {name.ljust(width)}  {score.rjust(5)}"
                add(f"{line}  {paint(note, 'dim')}" if note else line)

        signals = view.signals()
        if signals:
            add("")
            add(paint("   Deterministic signals", "bold"))
            for severity, name, node, detail in signals:
                tone = "fail" if severity == "fail" else "warn"
                add(
                    f"     {paint(severity.ljust(4), tone)} {name} "
                    f"{paint(f'({node})', 'dim')} {detail}"
                )

        terminal = view.evidence.get("terminal_verdict")
        if isinstance(terminal, dict) and terminal.get("reasoning"):
            add("")
            add(paint("   Terminal verdict", "bold"))
            state = "bad" if terminal.get("bad") else "ok"
            add(f"     {state} — {terminal['reasoning']}")

        notes = view.notes()
        if notes:
            add("")
            add(paint("   Notes", "bold"))
            for note in notes:
                add(f"     - {note}")

        cost = view.report.get("downstream_cost_usd")
        if isinstance(cost, (int, float)) and cost > 0:
            add("")
            add(paint(f"   downstream cost of the fault: ${cost:.4f}", "dim"))

    add("")
    incidents = run.incidents
    unverified = unverified_graphs(run)
    if incidents:
        add(
            paint(
                f"{len(incidents)} incident(s) across {len(run.graphs)} graph(s)",
                "bold",
                "fail",
            )
        )
    elif unverified:
        # Deliberately not green: nothing was measured on these graphs, so
        # there is no pass to report.
        add(
            paint(
                f"{len(unverified)} of {len(run.graphs)} graph(s) could not be verified "
                "— no incident, but no evidence either",
                "bold",
                "unknown",
            )
        )
    else:
        add(paint(f"no incidents across {len(run.graphs)} graph(s)", "bold", "ok"))
    if not run.judge_enabled:
        add(
            paint(
                "The judged channel did not run, so quality verdicts rest on the "
                "deterministic channel alone. Set JUDGE_BASE_URL and JUDGE_MODEL "
                "to enable per-node quality judging.",
                "dim",
            )
        )
    return "\n".join(out)


def render_markdown(run: AnalysisRun, source: str) -> str:
    """A findings brief a coding agent can act on (the CLI's `Export .md`)."""
    lines: list[str] = [f"# Agent Detective — findings for `{source}`", ""]
    lines.append(
        f"- **Graphs:** {len(run.graphs)}  ·  **Incidents:** {len(run.incidents)}"
        f"  ·  **Unverified:** {len(unverified_graphs(run))}"
    )
    lines.append(
        f"- **Judged channel:** {'on — ' + run.judge if run.judge_enabled else 'off — ' + run.judge}"
    )
    lines.append("")

    for graph in run.graphs:
        view = _GraphView(graph)
        verdict = view.verdict
        lines.append(f"## Graph `{graph.graph_id}`")
        lines.append("")
        lines.append(f"**{verdict.label}** — {verdict.template}")
        lines.append("")
        if view.report_type:
            lines.append(f"- Report type: `{view.report_type}`")
        confidence = view.report.get("confidence")
        if isinstance(confidence, (int, float)):
            lines.append(f"- Confidence: {_pct(float(confidence))}")
        if graph.bundle.graph_type:
            lines.append(f"- Graph type: `{graph.bundle.graph_type}`")
        lines.append("")

        if view.culprits:
            lines.append(f"### {culprit_heading(view.report_type, len(view.culprits) > 1)}")
            lines.append("")
            for culprit in view.culprits:
                lines.append(f"- `{view.label(culprit)}`")
            lines.append("")

        if view.defects:
            lines.append("### Defects")
            lines.append("")
            for defect in view.defects:
                origin = defect.get("origin") or {}
                desc = defect_descriptor(str(defect.get("kind")), origin)
                where = origin_phrase(origin, view.label)
                lines.append(f"- **{desc.label}** — {desc.template.replace('{origin}', where)}")
                lines.append(
                    f"  - observation {_pct(defect.get('observation_confidence'))}, "
                    f"attribution {_pct(defect.get('attribution_confidence'))}, "
                    f"channel `{defect.get('channel', '-')}`"
                )
                for ref in defect.get("finding_refs") or []:
                    line = view.finding_line(int(ref.get("ref", -1)))
                    if line:
                        lines.append(f"  - {ref.get('role', 'context')}: {line}")
                for caveat in view.caveats(defect):
                    lines.append(f"  - caveat: {caveat}")
            lines.append("")

        rows = view.node_rows()
        if rows:
            lines.append("### Pipeline")
            lines.append("")
            lines.append("| node | score | notes |")
            lines.append("| --- | --- | --- |")
            for name, score, note in rows:
                lines.append(f"| `{name}` | {score} | {note or ''} |")
            lines.append("")

        signals = view.signals()
        if signals:
            lines.append("### Deterministic signals")
            lines.append("")
            for severity, name, node, detail in signals:
                lines.append(f"- `{severity}` **{name}** at `{node}` — {detail}")
            lines.append("")

        notes = view.notes()
        if notes:
            lines.append("### Notes")
            lines.append("")
            for note in notes:
                lines.append(f"- {note}")
            lines.append("")

    if not run.judge_enabled:
        lines.append("---")
        lines.append("")
        lines.append(
            "The judged channel did not run: these findings rest on the "
            "deterministic channel alone (rules over the trace payloads). "
            "Set `JUDGE_BASE_URL` and `JUDGE_MODEL` to add per-node quality "
            "judging."
        )
        lines.append("")
    return "\n".join(lines)


def render_json(run: AnalysisRun, source: str) -> dict[str, Any]:
    """The full verdict as data — for CI, diffing, or feeding another tool."""
    graphs: list[dict[str, Any]] = []
    for graph in run.graphs:
        view = _GraphView(graph)
        verdict = graph.verdict
        graphs.append(
            {
                "graph_id": str(graph.graph_id),
                "graph_type": graph.bundle.graph_type,
                "run_count": len(graph.bundle.runs),
                "verdict": view.verdict.label,
                "measured": view.measured,
                "report_type": view.report_type,
                "confidence": view.report.get("confidence"),
                "culprits": [view.label(c) for c in view.culprits],
                "culprit_run_ids": [str(c) for c in view.culprits],
                "incident": None
                if graph.incident is None
                else {
                    "key": graph.incident["incident_key"],
                    "trigger": graph.incident["trigger"],
                },
                "tier1": None
                if verdict is None
                else {
                    "terminal_verdict": verdict.terminal_judge_verdict,
                    "terminal_score": verdict.terminal_judge_score,
                    "flags": list(verdict.flags),
                    "flagged": verdict.flagged,
                },
                "tier2_ran": graph.tier2_ran,
                "evidence": view.evidence,
            }
        )
    return {
        "source": source,
        "judge": {"enabled": run.judge_enabled, "description": run.judge},
        "incidents": len(run.incidents),
        "graphs": graphs,
    }


def write(text: str, stream: TextIO | None = None) -> None:
    print(text, file=stream or sys.stdout)
