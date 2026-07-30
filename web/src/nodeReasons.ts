// Per-run failure reasons extracted from a blame report's Evidence.
// NodeDetails renders these as the "Why this score" section: each reason says
// WHY the node scored low and `example` shows the concrete failure exhibit.
//
// All fields are read defensively (evidence from older reports may lack them);
// a missing source simply contributes no reasons.

import type { Evidence } from "./api/types";

export type NodeReasonKind =
  | "flag"
  | "signal"
  | "contract"
  | "loop"
  | "judge"
  | "candidacy"
  | "override";

export interface NodeReason {
  kind: NodeReasonKind;
  severity: "fail" | "warn" | "info";
  title: string;
  detail: string | null;
  // The concrete exhibit: the exact value/excerpt where the node failed.
  example: string | null;
}

const SEVERITY_RANK: Record<NodeReason["severity"], number> = {
  fail: 0,
  warn: 1,
  info: 2,
};

// Human text for a node's `unscored_reason`. `deliberate` marks an outcome that
// is CORRECT by design — the measurement was withheld on purpose, not lost — so
// the UI can keep it out of the error register.
export interface UnscoredExplanation {
  title: string;
  detail: string | null;
  deliberate: boolean;
}

const UNSCORED_REASONS: Record<string, UnscoredExplanation> = {
  judge_skipped_deterministic_node: {
    title: "Judge skipped — deterministic node",
    detail:
      "The trace declares this node as making no LLM call, so the quality judge was deliberately not run. With no registered output contract the remaining channels stayed below the scoring floor, so no composite was blended. This is the intended outcome, not a missing measurement — the node's deterministic signals still stand.",
    deliberate: true,
  },
  judge_budget_exhausted: {
    title: "Judge stopped — spend cap reached",
    detail:
      "JUDGE_MAX_SPEND_USD was used up before this node was judged, so the judged channel is absent by an operator's decision rather than by fault. Raise the cap and re-analyze to score it; the deterministic signals below were measured either way.",
    deliberate: true,
  },
  insufficient_components: {
    title: "Too few channels to blend",
    detail:
      "The channels that reported carried less weight than the scoring floor, so no composite was produced — one cheap channel is not a quality verdict.",
    deliberate: false,
  },
  payload_missing: {
    title: "No output payload recorded",
    detail:
      "Nothing was captured for this run, so there was nothing to score — an instrumentation gap, not the agent's fault.",
    deliberate: false,
  },
  empty_output: {
    title: "Output recorded empty",
    detail:
      "The exporter worked and captured an empty output while usage reported tokens. The observation rides the deterministic channel; the score stays unknown rather than being invented.",
    deliberate: false,
  },
  zero_result_set: {
    title: "Well-formed output, no records",
    detail:
      "The output parses and carries nothing to report. A judge rates emptiness as worthless, so no number was produced from an observed absence.",
    deliberate: true,
  },
  judge_error: {
    title: "Judge call failed",
    detail: "The judge did not return a usable verdict for this run.",
    deliberate: false,
  },
  not_analyzed: {
    title: "Never analyzed",
    detail: "No analysis has run over this graph yet.",
    deliberate: true,
  },
};

// Unknown codes surface verbatim rather than being dropped or dressed up.
export function explainUnscored(
  reason: string | null | undefined,
): UnscoredExplanation | null {
  if (!reason) return null;
  return UNSCORED_REASONS[reason] ?? { title: reason, detail: null, deliberate: false };
}

function renderValue(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value);
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

export function collectNodeReasons(runId: string, evidence: Evidence | null): NodeReason[] {
  if (!evidence) return [];
  const reasons: NodeReason[] = [];

  // Deterministic signals are the strongest evidence: named reproducible checks
  // (never the LLM judge) with detail + basis sentences.
  for (const signal of evidence.deterministic_signals ?? []) {
    if (signal.run_id !== runId) continue;
    reasons.push({
      kind: "signal",
      severity: signal.severity === "fail" ? "fail" : "warn",
      title: signal.name,
      detail: signal.detail,
      example: signal.basis || null,
    });
  }

  // Contract violations: a carried-through parameter this node silently
  // rewrote — the from→to diff is the exact failure exhibit.
  for (const violation of evidence.contract_violations ?? []) {
    if (violation.run_id !== runId) continue;
    reasons.push({
      kind: "contract",
      severity: "fail",
      title: `Contract breach: ${violation.key}`,
      detail: "A carried-through parameter was silently rewritten by this node.",
      example: `${violation.key}: ${renderValue(violation.from)} → ${renderValue(violation.to)}`,
    });
  }

  // Loop anomalies the node participates in.
  for (const loop of evidence.loop_anomalies ?? []) {
    if (!loop.member_run_ids.includes(runId)) continue;
    reasons.push({
      kind: "loop",
      severity: "fail",
      title: "Loop detected",
      detail: `This run is part of a loop: ${loop.agent_names.join(" → ")}.`,
      example: `${loop.iterations} iterations (${loop.limit_kind})`,
    });
  }

  // Structured per-node flags from scoring (each caps the judge component).
  for (const flag of evidence.node_flags?.[runId] ?? []) {
    reasons.push({
      kind: "flag",
      severity: "warn",
      title: `Flag: ${flag}`,
      detail: null,
      example: null,
    });
  }

  // Ground-truth score corrections.
  for (const override of evidence.score_overrides ?? []) {
    if (override.run_id !== runId) continue;
    reasons.push({
      kind: "override",
      severity: "warn",
      title: "Score overridden",
      detail: override.reason,
      example: `${override.original ?? "—"} → ${override.effective}`,
    });
  }

  // The judge's own rationale for this run.
  const judgeNote = evidence.judge_notes?.[runId];
  if (judgeNote) {
    reasons.push({
      kind: "judge",
      severity: "info",
      title: "Judge rationale",
      detail: judgeNote,
      example: null,
    });
  }

  // Why the engine did or did not blame this node. Prefer the typed record's
  // verdict code as the title; the prose string is the detail.
  const candidacyRecord = evidence.candidacy_records?.[runId];
  const candidacyNote = evidence.candidacy?.[runId];
  if (candidacyRecord || candidacyNote) {
    reasons.push({
      kind: "candidacy",
      severity: "info",
      title: `Attribution: ${candidacyRecord?.verdict ?? "—"}`,
      detail: candidacyNote ?? null,
      example: null,
    });
  }

  return reasons.sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]);
}
