// Typed model of schema-2 evidence (verdict-refactor-plan.md §2.1 / §2.2).
//
// These mirror the engine's Finding / Defect / Origin output. The engine side
// is built in parallel (Track A / F1); field names here follow the plan's
// contract and the repo convention (snake_case, matching the JSON wire format
// used throughout web/src/api/types.ts). Downstream screens render against
// these types; nothing here interprets or generates prose (that is descriptor.ts).

// ---------------------------------------------------------------------------
// Shared vocabulary (§2.2, §3)
// ---------------------------------------------------------------------------

// Where a finding's basis comes from: a reproducible rule vs a judged
// assessment. Neither channel gets an epistemic title — "deterministic" says
// the rule fired reproducibly, not that its reference is beyond dispute
// (§2.4 certainty taxonomy, revised: "ground truth" is banned).
export type Channel = "deterministic" | "judged";

// The five catalogued defect kinds (§2.2).
export type DefectKind = "contract" | "content" | "form" | "loop" | "verification";

// Report types derived from the defect set (§3). Mirrors ReportType in
// api/types.ts; kept for humans/alerting, no longer load-bearing.
export type ReportType =
  | "cut_point"
  | "multi_culprit"
  | "composition_failure"
  | "loop_detected"
  | "root_cause_external"
  | "verification_gap"
  | "degraded_recovered"
  | "shipped_with_latent_defect"
  | "terminal_defect_unlocalized"
  | "unclassified";

// ---------------------------------------------------------------------------
// Finding — a typed fact with provenance (§2.1)
// ---------------------------------------------------------------------------

// What the finding is about. A deliverable check fires on `terminal`; a
// node-scoped check on a specific run; a graph-shape check on `graph`.
export type SubjectScope = "run" | "terminal" | "graph";

export interface Subject {
  scope: SubjectScope;
  // Present when scope === "run" (the node the finding measures).
  run_id?: string | null;
}

// Where the reference / basis came from (§2.1). For a judged finding this is
// the prompt hash; for a deterministic one the rule fingerprint; for a
// requirement it is the verbatim quote plus its source.
export type ProvenanceSource = "user_request" | "harness_state" | "upstream";

export interface Provenance {
  // Which member of the sum type: "UserRequest" | "HarnessState" | "Upstream" |
  // "RuleFingerprint" | "JudgePrompt".
  kind?: string;
  // Rendered label — the engine's own `provenance_label`, carried on the wire
  // so the UI needs no copy of the code→phrase table. Display this; the codes
  // below are for branching, never for reading.
  label?: string | null;
  // Stable identifiers of the basis: the rule fingerprint and the source code
  // (`per_node_quality_judge`, `terminal_judge_form`, …). Opaque here.
  rule?: string | null;
  detail?: string | null;
  // For requirement-derived findings: where the requirement came from.
  source?: ProvenanceSource | null;
  // Verbatim requirement quote, when the finding carries one (terminal split).
  quote?: string | null;
}

export interface Finding {
  // Catalogued finding kind (§2.1 table: contract_breach, content_score,
  // required_section_missing, verifier_verdict, loop_anomaly, …). Kept as a
  // string: adding a detector must not require a type change here.
  kind: string;
  channel: Channel;
  subject: Subject;
  // Kind-specific typed payload. Opaque at this layer; each renderer narrows it.
  data: Record<string, unknown>;
  provenance: Provenance;
  // 1.0 for deterministic; calibrated for judged (§2.1).
  certainty: number;
  // Findings measuring the SAME fact share this key so the reconciler can pair
  // them (§2.4 fact identity). Absent when the finding measures nothing shared.
  fact_key?: string | null;
}

// ---------------------------------------------------------------------------
// Origin — a sum type (§2.2)
// ---------------------------------------------------------------------------
//
// A defect without a candidate is `Unlocalized` BY CONSTRUCTION — the run-C
// class of bug (a cut_point whose evidence shows no content candidate) is an
// unrepresentable state here, not a branch to remember.

export type Origin =
  // Attributed to a specific node.
  | { kind: "localized"; run_id: string }
  // Observed but not attributable to any node. `reason` is a stable CODE
  // (`no_content_candidate`, `orchestration_layer`, …); `reason_label` is its
  // one phrasing, resolved below.
  | { kind: "unlocalized"; reason: string; reason_label?: string }
  // Entered from outside the observed graph (bad input).
  | { kind: "external" }
  // No node owns the check — a gap in the graph's design, not any node's work
  // (e.g. "no verifier owns contract/form vision").
  | { kind: "design"; reason?: string | null; reason_label?: string };

export type OriginKind = Origin["kind"];

// ---------------------------------------------------------------------------
// Defect — an interpreted fault with its own origin resolution (§2.2)
// ---------------------------------------------------------------------------

// Structured caveats (§2.4). Rendered as chips — a caveat is a FIELD, never
// free prose, so it can never be truncated away mid-sentence.
export type DefectCaveatKind =
  // The confidence baseline is assumed, not measured.
  | "base_assumed"
  // The defect sits at the edge of what this channel can observe.
  | "observability_boundary"
  // Present in one channel but unverified in the other.
  | "unverified_in_channel"
  // The node underperformed but successors + terminal recovered (near-miss).
  | "recovered";

export interface DefectCaveat {
  kind: DefectCaveatKind;
  // Short human detail rendered inside the chip's tooltip.
  detail?: string | null;
}

// A confidence is always a {claim, value} pair (§2.4). Defects carry both an
// observation and (optionally) an attribution reading; the UI renders both,
// never collapsing them into a single ambiguous number.
export type ConfidenceClaim = "observed" | "attributed";

export interface Confidence {
  claim: ConfidenceClaim;
  value: number;
}

// A finding reference carries POLARITY: what the cited finding shows for this
// defect. "supporting" asserts it, "refuting" is counter-evidence kept visible
// (a defect must never render exculpatory findings as "the evidence for it"),
// "context" is a related measurement. Legacy payloads stored bare indices —
// those normalize to "context" (their polarity was never classified).
export type FindingRefRole = "supporting" | "refuting" | "context";

export interface FindingRef {
  ref: number;
  role: FindingRefRole;
}

export interface Defect {
  kind: DefectKind;
  // Typed refs into the report's findings[]. The engine guarantees ≥1
  // supporting ref per defect (§2.4 no-unsupported, validated at build).
  finding_refs: FindingRef[];
  channel: Channel;
  origin: Origin;
  // Is the output defective?
  observation_confidence: number;
  // Did it originate at `origin`? null when the defect is unattributed
  // (e.g. origin is Unlocalized/External).
  attribution_confidence: number | null;
  // Downstream run_ids the defect flowed through.
  propagation: string[];
  // Auditable caveats, rendered as chips (§2.4).
  caveats?: DefectCaveat[];
}

// ---------------------------------------------------------------------------
// Projection — the derived, render-ready verdict (§2.3)
// ---------------------------------------------------------------------------

export interface SchemaTwoEvidence {
  schema: 2;
  findings: Finding[];
  defects: Defect[];
  // Derived by derive_report_type(defects) — the only place that knows the
  // mapping. Present for humans/alerting/continuity.
  report_type: ReportType;
}

// Type guard: is this evidence blob the new schema-2 shape? Screens gate the
// new renderer on this (§2.5) and fall back to the legacy renderer otherwise.
export function isSchemaTwo(evidence: unknown): evidence is SchemaTwoEvidence {
  return (
    typeof evidence === "object" &&
    evidence !== null &&
    (evidence as { schema?: unknown }).schema === 2 &&
    Array.isArray((evidence as { defects?: unknown }).defects) &&
    Array.isArray((evidence as { findings?: unknown }).findings)
  );
}

// The ENGINE serializes with Python-native shapes that differ from the typed
// wire model the UI renders against: `origin.kind` is capitalized
// (`"Localized"`), `subject` is a string (`"run:<id>"`, `"terminal"`,
// `"graph"`), and caveats are FLAT boolean/string fields on the defect rather
// than a `caveats[]` array. This adapter normalizes the raw evidence into the
// typed shape ONCE at the boundary, so every component downstream can rely on
// the clean model (and no component keys off the capitalized names). Returns
// null when the blob is not schema-2.
function normalizeOrigin(raw: unknown): Origin {
  const o = (raw ?? {}) as { kind?: unknown; run_id?: unknown; reason?: unknown };
  const kind = String(o.kind ?? "").toLowerCase();
  const reason = typeof o.reason === "string" ? o.reason : "";
  switch (kind) {
    case "localized":
      return { kind: "localized", run_id: String(o.run_id ?? "") };
    case "external":
      return { kind: "external" };
    case "design":
      return {
        kind: "design",
        reason: reason || null,
        reason_label: originReasonLabel(reason),
      };
    case "unlocalized":
      return { kind: "unlocalized", reason, reason_label: originReasonLabel(reason) };
    default:
      return {
        kind: "unlocalized",
        reason: reason || "origin not localized",
        reason_label: originReasonLabel(reason) || "origin not localized",
      };
  }
}

// Origin reason CODES → their one phrasing, mirroring the engine's narrative
// table. Pre-collapse reports stored prose here; an unknown code passes through
// verbatim, so those keep reading exactly as they did.
const ORIGIN_REASONS: Record<string, string> = {
  orchestration_layer:
    "no node individually failed; the fault enters at the orchestration/task-design layer",
  no_content_candidate:
    "terminal content is bad but no node qualifies as a content origin (deterministic-only candidate scored healthy, successors recovered)",
  input_already_flawed: "input entered the graph already flawed",
  no_form_verifier:
    "no verifier charter in this graph covers form/contract vision (verifier charters cover content)",
};

export function originReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "";
  return ORIGIN_REASONS[reason] ?? reason;
}

function normalizeSubject(raw: unknown): Subject {
  if (typeof raw === "string") {
    if (raw.startsWith("run:")) return { scope: "run", run_id: raw.slice(4) };
    if (raw === "terminal") return { scope: "terminal" };
    return { scope: "graph" };
  }
  const s = (raw ?? {}) as Subject;
  return s.scope ? s : { scope: "graph" };
}

function normalizeRefs(raw: unknown): FindingRef[] {
  if (!Array.isArray(raw)) return [];
  const out: FindingRef[] = [];
  for (const r of raw) {
    if (typeof r === "number") {
      out.push({ ref: r, role: "context" });
    } else if (typeof r === "object" && r !== null && typeof (r as { ref?: unknown }).ref === "number") {
      const role = (r as { role?: unknown }).role;
      out.push({
        ref: (r as { ref: number }).ref,
        role: role === "supporting" || role === "refuting" ? role : "context",
      });
    }
  }
  return out;
}

function normalizeCaveats(raw: Record<string, unknown>): DefectCaveat[] {
  const out: DefectCaveat[] = [];
  if (raw.base_assumed) out.push({ kind: "base_assumed" });
  if (raw.observability_boundary) out.push({ kind: "observability_boundary" });
  if (typeof raw.unverified_in_channel === "string" && raw.unverified_in_channel) {
    out.push({
      kind: "unverified_in_channel",
      detail: `unverified in the ${raw.unverified_in_channel} channel`,
    });
  }
  if (raw.recovered) out.push({ kind: "recovered" });
  return out;
}

export function toSchemaTwo(evidence: unknown): SchemaTwoEvidence | null {
  if (!isSchemaTwo(evidence)) return null;
  const raw = evidence as unknown as {
    schema: 2;
    report_type: ReportType;
    findings: Array<Record<string, unknown>>;
    defects: Array<Record<string, unknown>>;
  };
  return {
    schema: 2,
    report_type: raw.report_type,
    findings: raw.findings.map((f) => ({
      ...(f as unknown as Finding),
      subject: normalizeSubject(f.subject),
    })),
    defects: raw.defects.map((d) => ({
      ...(d as unknown as Defect),
      origin: normalizeOrigin(d.origin),
      finding_refs: normalizeRefs(d.finding_refs),
      caveats:
        Array.isArray(d.caveats) && d.caveats.length > 0
          ? (d.caveats as DefectCaveat[])
          : normalizeCaveats(d),
    })),
  };
}
