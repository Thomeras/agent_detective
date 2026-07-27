// Runs-list verdict derivation (verdict-refactor-plan.md §9.2).
//
// The list endpoint (`api.listGraphs`) returns GraphSummary with NO verdict
// field, and the server is out of this phase's file ownership (web/** only).
// So the verdict is derived web-side by joining graphs with the incidents list
// (`api.listIncidents`), whose IncidentSummary.latest_report carries the typed
// `report_type` + confidence the projection already stored. No server change,
// no per-graph N+1 detail fetch.
//
// Everything here keys off TYPED enums (ReportType, DefectKind, Origin kind) —
// never off note-string prefixes (§1 / constraint 4). Pure functions, no I/O.

import type { GraphSummary, IncidentSummary, ReportType } from "../api/types";
import type { Defect, DefectKind } from "./types";
import {
  PASSED_VERDICT,
  UNANALYSED_VERDICT,
  originQualifier,
  toneForOrigin,
  verdictDescriptor,
  type Descriptor,
  type Tone,
} from "./descriptor";

// A compact defect pill for the list / card header. `key` is a stable, typed
// identity (kind + origin qualifier + optional node) so React never keys off a
// prose prefix. `label` is what the operator reads: `contract`, `content?U`,
// `content@think`.
export interface DefectChip {
  key: string;
  label: string;
  tone: Tone;
}

// The verdict a runs-list row answers with: the badge + its confidence + the
// defect chips that summarise WHAT broke. One object per graph.
export interface RunVerdict {
  descriptor: Descriptor;
  confidence: number | null;
  chips: DefectChip[];
}

// ---------------------------------------------------------------------------
// Incident selection — which incident speaks for a graph
// ---------------------------------------------------------------------------

// Group incidents by graph_id, then pick the one that carries the live verdict:
// the most recently updated NON-superseded incident, falling back to the most
// recent overall when every incident for the graph is superseded (Track C #2).
export function incidentByGraph(
  incidents: IncidentSummary[],
): Map<string, IncidentSummary> {
  const byGraph = new Map<string, IncidentSummary[]>();
  for (const inc of incidents) {
    const list = byGraph.get(inc.graph_id) ?? [];
    list.push(inc);
    byGraph.set(inc.graph_id, list);
  }

  const chosen = new Map<string, IncidentSummary>();
  for (const [graphId, list] of byGraph) {
    const live = pickLiveIncident(list);
    if (live) chosen.set(graphId, live);
  }
  return chosen;
}

function pickLiveIncident(list: IncidentSummary[]): IncidentSummary | null {
  if (list.length === 0) return null;
  const byRecency = [...list].sort(
    (a, b) => updatedMs(b) - updatedMs(a),
  );
  const active = byRecency.find((i) => i.status !== "superseded");
  return active ?? byRecency[0];
}

function updatedMs(inc: IncidentSummary): number {
  const t = Date.parse(inc.updated_at ?? inc.created_at ?? "");
  return Number.isNaN(t) ? 0 : t;
}

// ---------------------------------------------------------------------------
// Per-graph verdict
// ---------------------------------------------------------------------------

// Derive the row verdict for one graph, given the incident that speaks for it
// (or null when the graph raised none).
//
//   incident with a report  -> the projected verdict (verdictDescriptor)
//   no incident, finalized   -> PASSED (analysed, clean, nothing raised)
//   no incident, still active -> UNANALYSED (analysis has not run yet)
export function runVerdictFor(
  graph: GraphSummary,
  incident: IncidentSummary | null | undefined,
): RunVerdict {
  const report = incident?.latest_report ?? null;
  if (report && report.report_type) {
    return {
      descriptor: verdictDescriptor(report.report_type),
      confidence: report.confidence,
      chips: chipsFromReportType(report.report_type),
    };
  }
  if (graph.status === "finalized") {
    return { descriptor: PASSED_VERDICT, confidence: null, chips: [] };
  }
  return { descriptor: UNANALYSED_VERDICT, confidence: null, chips: [] };
}

// ---------------------------------------------------------------------------
// Chips from a report_type (list view — no evidence loaded)
// ---------------------------------------------------------------------------
//
// The list only has the projected `report_type` (defects[] live in the full
// evidence, fetched lazily in the detail view). This maps the typed report_type
// to the representative defect kind(s) + origin qualifier it implies — the same
// mapping the engine's derivation table encodes (§3), read backwards for a
// glanceable summary. Node identity is intentionally omitted here (the list has
// no node labels); the detail card renders `@node` from the real Defect.

type ChipSpec = { kind: DefectKind; qualifier: "" | "U" | "X" | "D"; tone: Tone };

const REPORT_TYPE_CHIPS: Record<ReportType, ChipSpec[]> = {
  cut_point: [{ kind: "content", qualifier: "", tone: "fail" }],
  multi_culprit: [{ kind: "content", qualifier: "", tone: "fail" }],
  composition_failure: [{ kind: "content", qualifier: "U", tone: "unknown" }],
  loop_detected: [{ kind: "loop", qualifier: "", tone: "fail" }],
  root_cause_external: [{ kind: "content", qualifier: "X", tone: "warn" }],
  verification_gap: [{ kind: "verification", qualifier: "", tone: "fail" }],
  degraded_recovered: [{ kind: "content", qualifier: "", tone: "warn" }],
  shipped_with_latent_defect: [{ kind: "contract", qualifier: "", tone: "fail" }],
  terminal_defect_unlocalized: [
    { kind: "contract", qualifier: "", tone: "fail" },
    { kind: "content", qualifier: "U", tone: "unknown" },
  ],
  unclassified: [],
};

function chipsFromReportType(reportType: ReportType): DefectChip[] {
  const specs = REPORT_TYPE_CHIPS[reportType] ?? [];
  return specs.map((s) => ({
    key: `${s.kind}?${s.qualifier || "L"}`,
    label: chipLabel(s.kind, s.qualifier),
    tone: s.tone,
  }));
}

function chipLabel(kind: DefectKind, qualifier: "" | "U" | "X" | "D"): string {
  return qualifier ? `${kind}?${qualifier}` : kind;
}

// ---------------------------------------------------------------------------
// Chips from a real Defect (detail view — evidence loaded)
// ---------------------------------------------------------------------------

// A precise chip for one schema-2 Defect: `contract@think` when localized (the
// node label comes from `runLabel`), `content?U` / `content?X` / `content?D`
// otherwise. Tone follows the defect's origin kind (localized fault = fail,
// unlocalized = unknown, external/design = warn).
export function defectChip(
  defect: Defect,
  index: number,
  runLabel?: (runId: string) => string,
): DefectChip {
  const tone = toneForOrigin(defect.origin.kind);
  let label: string;
  if (defect.origin.kind === "localized") {
    const node = runLabel ? runLabel(defect.origin.run_id) : defect.origin.run_id;
    label = `${defect.kind}@${node}`;
  } else {
    label = chipLabel(defect.kind, originQualifier(defect.origin.kind));
  }
  // Index keeps the key unique when two defects share kind+origin.
  return { key: `${defect.kind}-${defect.origin.kind}-${index}`, label, tone };
}
