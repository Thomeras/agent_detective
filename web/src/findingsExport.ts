// Export a graph's blame findings as a Markdown brief a coding agent can act on:
// the origin (where quality broke), per-node judge criticism, verification gaps,
// fact propagation and loop info — everything needed to locate and fix the bug.

import type { GraphDetail, ReportDetail, RunNodeData } from "./api/types";

function fmtScore(s: number | null | undefined): string {
  return s === null || s === undefined ? "unknown" : s.toFixed(2);
}

function detectLoopChain(graph: GraphDetail, nameOf: (id: string) => string): string[] {
  // Nodes that lie on a cycle, ordered by start time — the retry loop members.
  const ids = graph.nodes.map((n) => n.data.id);
  const adj = new Map<string, string[]>();
  ids.forEach((id) => adj.set(id, []));
  graph.edges.forEach((e) => adj.get(e.data.source)?.push(e.data.target));
  const reaches = (from: string, to: string): boolean => {
    const seen = new Set<string>();
    const stack = [...(adj.get(from) ?? [])];
    while (stack.length) {
      const cur = stack.pop()!;
      if (cur === to) return true;
      if (seen.has(cur)) continue;
      seen.add(cur);
      stack.push(...(adj.get(cur) ?? []));
    }
    return false;
  };
  const loop = graph.nodes
    .filter((n) => reaches(n.data.id, n.data.id))
    .sort((a, b) => Date.parse(a.data.started_at ?? "") - Date.parse(b.data.started_at ?? ""));
  return loop.map((n) => nameOf(n.data.id));
}

export function buildFindingsMarkdown(
  graph: GraphDetail,
  report: ReportDetail | null,
): string {
  const dataById = new Map<string, RunNodeData>(graph.nodes.map((n) => [n.data.id, n.data]));
  const nameOf = (id: string): string => dataById.get(id)?.agent_name ?? id.slice(0, 8);
  const ev = report?.evidence ?? null;
  const drops = ev?.drops ?? {};
  const scoreMap = ev?.score_map ?? {};
  const L: string[] = [];

  L.push(`# Agent Detective — Findings`);
  L.push("");
  L.push(
    `**Graph:** \`${graph.graph_id}\`  ·  **Status:** ${graph.status}  ·  ` +
      `**Runs:** ${graph.run_count ?? graph.nodes.length}  ·  ` +
      `**Cost:** ${graph.total_cost_usd != null ? "$" + graph.total_cost_usd.toFixed(4) : "-"}`,
  );
  L.push("");

  // --- Verdict ---
  L.push(`## Verdict`);
  if (report) {
    L.push(`- **Type:** \`${report.report_type ?? "unclassified"}\``);
    if (report.confidence != null) {
      L.push(`- **Confidence:** ${(report.confidence * 100).toFixed(0)}%`);
    }
    const origins = report.culprit_run_ids ?? [];
    if (origins.length) {
      const parts = origins.map((id) => {
        const drop = drops[id] != null ? `, dropped ${drops[id].toFixed(2)}` : "";
        return `\`${nameOf(id)}\` (composite score ${fmtScore(scoreMap[id])}${drop})`;
      });
      L.push(`- **Origin — where quality broke:** ${parts.join(", ")}`);
    }
    const manifest = ev?.manifestation_run_ids ?? [];
    if (manifest.length) {
      L.push(`- **Manifested at (terminal):** ${manifest.map((id) => `\`${nameOf(id)}\``).join(", ")}`);
    }
    const gaps = ev?.verification_gaps ?? [];
    if (gaps.length) {
      L.push(
        `- **Verification gaps:** ${gaps
          .map((g) => `\`${g.agent_name}\``)
          .join(", ")} — passed the work while the final output was bad`,
      );
    }
  } else {
    L.push(`- No blame report on this graph (no incident).`);
  }
  L.push("");

  // --- Loop ---
  const loopChain = detectLoopChain(graph, nameOf);
  if (loopChain.length) {
    L.push(`## Retry loop detected`);
    L.push(
      `Execution cycled through: ${loopChain.map((n) => `\`${n}\``).join(" → ")} → (back). ` +
        `The worst iteration is the lowest-scoring member below.`,
    );
    L.push("");
  }

  // --- Per-node quality ---
  L.push(`## Node quality`);
  L.push(`_Score is the blended composite (schema + judge + heuristics), not any single component._`);
  L.push(`| Agent | Composite score | Drop | Status | Tokens in/out | Cost |`);
  L.push(`|---|---|---|---|---|---|`);
  const rows = [...graph.nodes].sort(
    (a, b) => (a.data.quality_score ?? 2) - (b.data.quality_score ?? 2),
  );
  for (const n of rows) {
    const d = n.data;
    const drop = drops[d.id] != null ? `-${drops[d.id].toFixed(2)}` : "";
    const cost = d.cost_usd != null ? "$" + d.cost_usd.toFixed(4) : "";
    const tok = `${d.tokens_in ?? "-"}/${d.tokens_out ?? "-"}`;
    // An unscored node prints its reason, never a number it does not have.
    const score =
      d.quality_score == null && d.unscored_reason
        ? `unscored (${d.unscored_reason})`
        : fmtScore(d.quality_score);
    L.push(
      `| \`${d.agent_name ?? "-"}\` | ${score} | ${drop} | ${d.status} | ${tok} | ${cost} |`,
    );
  }
  L.push("");

  // --- Judge criticism per node (what to fix) ---
  // A judge sentence is paired with the judge's own component, never the
  // composite. When the two differ, both show with the claimed/effective
  // vocabulary of BlameReportPanel; when the judge component is missing
  // (unscored node, judge never ran) the export says so instead of borrowing
  // the composite.
  const judgeScoreOf = (id: string): string => {
    const d = dataById.get(id);
    const judge = d?.score_components?.judge;
    if (judge == null) {
      const why = d?.unscored_reason ? ` — node unscored (${d.unscored_reason})` : "";
      return `judge score not recorded${why}`;
    }
    const composite = scoreMap[id] ?? d?.quality_score;
    if (composite != null && fmtScore(composite) !== fmtScore(judge)) {
      return `claimed ${judge.toFixed(2)} → effective composite ${composite.toFixed(2)}`;
    }
    return `${judge.toFixed(2)}`;
  };
  const notes = ev?.judge_notes ?? {};
  const noteEntries = Object.entries(notes);
  if (noteEntries.length) {
    L.push(`## Judge findings (what is wrong)`);
    for (const [id, note] of noteEntries) {
      L.push(`- **${nameOf(id)}** (${judgeScoreOf(id)}): ${note}`);
    }
    L.push("");
  }

  // --- Fact propagation ---
  const facts = ev?.fact_propagation ?? [];
  if (facts && facts.length) {
    L.push(`## Fact propagation`);
    for (const f of facts) {
      const found = f.found_in.length ? f.found_in.map((id) => nameOf(id)).join(", ") : "none";
      const nc = f.not_checkable?.length ? ` · not checkable: ${f.not_checkable.map(nameOf).join(", ")}` : "";
      L.push(`- "${f.claim}" — found in: ${found}${nc}`);
    }
    L.push("");
  }

  // --- Action for a coding agent ---
  L.push(`## Suggested next step`);
  const origin = report?.culprit_run_ids?.[0];
  if (origin) {
    L.push(
      `Start at the origin node **\`${nameOf(origin)}\`** — that is where quality first ` +
        `broke. Use its judge finding above and inspect its input vs output. ` +
        (loopChain.length
          ? `It sits inside a retry loop, so also check why the loop did not recover.`
          : ``),
    );
  } else {
    L.push(`Review the lowest-scoring nodes in the table above.`);
  }
  L.push("");
  L.push(`---`);
  L.push(`_Generated by Agent Detective._`);
  return L.join("\n");
}

export function downloadText(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
