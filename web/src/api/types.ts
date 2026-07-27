// TypeScript mirrors of the JSON returned by services/api.
// Field names and nesting match services/api/api/serializers.py exactly.
// UUIDs, Decimals and datetimes are serialized to strings/numbers by
// serializers.json_value, so they surface here as `string | number`.

export type GraphStatus = "active" | "finalized";
export type RunStatus = "ok" | "degraded" | "failed";
export type EdgeType = "SPAWN" | "A2A_MESSAGE" | "TOOL_DELEGATION";
// "superseded" is machine-set: a later completed analysis of the same graph
// reclassified the run (or came back clean), so this incident's class is stale.
export type IncidentStatus = "open" | "acknowledged" | "resolved" | "superseded";
export type IncidentTrigger =
  | "terminal_failure"
  | "degraded_quality"
  | "cost_overrun"
  | "loop_detected"
  | "latent_defect"
  | "manual";

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

// GET /graphs -> { graphs: GraphSummary[], limit, offset }
export interface GraphSummary {
  id: string;
  graph_id: string;
  name: string | null;
  graph_type: string | null;
  status: GraphStatus;
  started_at: string | null;
  ended_at: string | null;
  total_cost_usd: number | null;
  run_count: number | null;
}

export interface GraphListResponse {
  graphs: GraphSummary[];
  limit: number;
  offset: number;
}

// Cytoscape-shaped node element (serializers.run_node).
export interface RunNodeData {
  id: string;
  agent_name: string | null;
  agent_version: string | null;
  // Per-run identity (docs/deterministic-signals.md, B1). Nullable rather than
  // optional: the serializer reads them with row.get, so rows ingested before
  // migration 0006 still arrive as explicit nulls, never absent keys.
  model_name: string | null;
  prompt_hash: string | null;
  parent_run_id: string | null;
  trace_id: string | null;
  status: RunStatus;
  quality_score: number | null;
  score_components: Record<string, number | null> | null;
  unscored_reason: string | null;
  input_flawed: boolean | null;
  cost_usd: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  started_at: string | null;
  ended_at: string | null;
  input_summary: string | null;
  output_summary: string | null;
}

export interface RunNode {
  data: RunNodeData;
}

export interface RunEdgeData {
  id: string;
  source: string;
  target: string;
  type: EdgeType;
  detection_method: string | null;
}

export interface RunEdge {
  data: RunEdgeData;
}

// GET /graphs/{id} -> GraphSummary + { finalized_at, nodes, edges }
export interface GraphDetail extends GraphSummary {
  finalized_at: string | null;
  nodes: RunNode[];
  edges: RunEdge[];
}

// GET /graphs/{id}/payloads/{run_id}
export interface PayloadSide {
  source: "inline" | "overflow" | "none";
  content: string | null;
  bytes: number | null;
}

export interface RunPayloads {
  graph_id: string;
  run_id: string;
  input: PayloadSide;
  output: PayloadSide;
}

// POST /graphs/{id}/analyze
export interface AnalyzeResponse {
  dedup_key: string;
  stream_id: string;
}

// Frozen topology-classification contract (blame_engine/topology.py; the
// presentational client-side mirror lives in web/src/topology.ts). Advisory
// only — it never changes report_type, confidence, culprits or candidacy.
export type TopologyPrimary =
  | "disconnected"
  | "single_node"
  | "mesh"
  | "pipeline_with_feedback"
  | "cyclic_graph"
  | "pipeline"
  | "star"
  | "hierarchy"
  | "dag";

export interface TopologyClassification {
  primary: TopologyPrimary;
  node_count: number;
  edge_count: number;
  // Weakly connected components.
  components: number;
  max_fan_out: number;
  // Longest path (in nodes) through the SCC-condensation DAG; single node = 1.
  depth: number;
  // Nontrivial SCCs only (size >= 2).
  scc_count: number;
  // A<->B pairs.
  bidirectional_pairs: number;
  // run_ids whose removal disconnects the UNDIRECTED graph; [] when n < 3.
  articulation_points: string[];
}

// Evidence JSONB (blame_engine Evidence dataclass).
export interface LoopBaseline {
  mean_iterations: number;
  std_iterations: number;
  sample_count: number;
}

export interface LoopAnomaly {
  member_run_ids: string[];
  agent_names: string[];
  iterations: number;
  limit_kind: "max_iterations" | "statistical";
  baseline: LoopBaseline | null;
}

export interface FactPropagationEntry {
  claim: string;
  found_in: string[];
  // Successors whose payload was missing (e.g. the node failed): we genuinely
  // could not check, which is not the same as "not found".
  not_checkable?: string[];
  // How many payloads were actually checkable — distinguishes "absent
  // everywhere" from "nothing to check".
  checked?: number;
  // "required" (registered requirement × producers — a negative here is the
  // headline evidence) | "structured" (deterministic field extraction) |
  // "llm" (claims prompt fallback for prose outputs).
  source?: string;
}

export interface VerificationGap {
  run_id: string;
  agent_name: string;
  // "verdict_scored_incorrect" — the role-aware judge scored the verdict wrong;
  // "passed_bad_terminal" — deduced: terminal is bad, this verifier passed it.
  basis?: string;
}

// Tier1 terminal-judge verdict the classification leaned on — the evidence
// behind any "terminal verdict is bad" claim.
export interface TerminalVerdictEvidence {
  bad: boolean;
  score: number | null;
  reasoning: string | null;
  // False when the terminal judge never saw the deliverable's content (only a
  // path, an orchestrator wrapper, or a verifier verdict). A not-checkable
  // verdict is NOT ground truth: `bad` must be rendered as "not verifiable",
  // never as a failure. Absent on older reports, so treat undefined as true.
  checkable?: boolean;
  // Qualifies an "ok" verdict that ground truth cannot fully certify (content
  // ok, carried contract breached/unverified) — must be rendered next to the
  // badge, or the header lies.
  caveat?: string | null;
  // Which tier decided: "deterministic" (tier0 deliverable checks, no LLM ran)
  // or "llm_judge" (tier1). Absent on older reports.
  decided_by?: string | null;
  // True when the verdict's deterministic basis no longer reproduces on the
  // current rules/payload — the verdict is UNRELIABLE (not ground truth) and
  // must be rendered as stale, never as a live "bad".
  stale?: boolean;
  // WHY it stopped reproducing, settled by the stored rule-set fingerprint
  // (migration 0008): rule change vs representation divergence vs unknown
  // (verdict predates stamping).
  stale_cause?: string | null;
  // The CONTRACT axis, independent of the content verdict: "nonconformant
  // (verified): …" | "restored downstream (verified)" | "unverified".
  contract_conformance?: string | null;
}

// Cumulative decline over 2+ consecutive edges (no single edge crossed the
// gap threshold, but the chain did).
export interface DegradationPath {
  path: string[];
  scores: number[];
  cumulative_drop: number;
}

// Typed classification rationale (verdict refactor §2.4). Prose is generated
// from these by the engine's narrative templates and stored alongside for
// grep/export; it is never parsed back.
export interface NoteRecord {
  slug: string;
  data: Record<string, unknown>;
}

export interface CandidacyRecord {
  verdict: string;
  data: Record<string, unknown>;
}

export interface Evidence {
  score_map: Record<string, number | null>;
  drops: Record<string, number>;
  judge_notes: Record<string, string>;
  error_span_ids: Record<string, string[]>;
  loop_anomalies: LoopAnomaly[];
  unknown_ancestors: string[];
  fact_propagation: FactPropagationEntry[] | null;
  notes: string[];
  // The TYPED originals of `notes` (schema 2). `slug` is the note's stable
  // identity and `data` the payload its sentence was rendered from. Branch on
  // these — NEVER on a note's prefix or wording: the sentence is a render
  // artifact and rewording it must not be able to break a consumer.
  note_records?: NoteRecord[];
  // Where the failure surfaced — the terminal artifact/output (verifier sinks
  // are mapped back to the producer whose work they judged).
  manifestation_run_ids?: string[];
  // Verifier nodes whose PASS was wrong (see VerificationGap.basis).
  verification_gaps?: VerificationGap[];
  // Per-node audit trail: why it was or wasn't blamed, with the numbers.
  candidacy?: Record<string, string>;
  // The TYPED originals of `candidacy` (schema 2): run_id -> verdict code +
  // the numbers the decision rested on. Same rule as note_records.
  candidacy_records?: Record<string, CandidacyRecord>;
  terminal_verdict?: TerminalVerdictEvidence | null;
  degradation_paths?: DegradationPath[];
  // Deterministic topological order — JSONB scrambles object key order, so
  // score_map/candidacy must be rendered in THIS order.
  topo_order?: string[];
  // Nodes recognised as verifiers/gates, for visual grouping.
  verifier_run_ids?: string[];
  // Structured per-node flags from scoring (e.g. "unverifiable_artifact").
  node_flags?: Record<string, string[]>;
  // Ground-truth corrections: {run_id, original, effective, reason} — e.g. a
  // verifier's "verdict correctness" score refuted by the terminal verdict.
  score_overrides?: {
    run_id: string;
    original: number | null;
    effective: number;
    reason: string;
  }[];
  // Competing ORIGIN hypotheses, present only when the independent evidence
  // streams disagree on where the fault started (a divergence/tension signal
  // raised a later render/export origin the engine could not rule out). Weights
  // sum to 1.0 with an explicit { origin: null } "unresolved" remainder. Its
  // mere presence means: do NOT trust the single headline origin/confidence.
  hypotheses?: {
    origin: string | null;
    agent?: string | null;
    // Stable code for WHY this origin is live ("reported_origin",
    // "later_producer", "unresolved"); `basis` is its one rendering.
    basis_code?: string;
    basis: string;
    weight: number;
  }[];
  // How sure the culprit's OUTPUT is defective (deterministic/severity signal).
  // Near-certain for a hard signal. Absent on older reports.
  observation_confidence?: number | null;
  // How sure the fault ORIGINATED at this node rather than inheriting a bad
  // input. This is the attribution of the VERDICT-CARRYING defect (contract
  // when present, content otherwise) — never a blend. Absent on older reports.
  attribution_confidence?: number | null;
  // Attribution PER DEFECT — a contract breach observes both sides
  // (near-certain origination) while a content defect at the observability
  // boundary is capped. The headline above equals the verdict-carrying entry.
  attribution_breakdown?: { defect: string; attribution: number; basis: string }[];
  // Engine findings refuted by later verified evidence (e.g. the near-miss
  // "degraded_recovered" note after escalation proved the breach shipped).
  // Same mechanism as score_overrides, applied to our OWN claims — the note
  // stays in the ledger, the UI marks it superseded.
  superseded_notes?: { slug: string; superseded_by: string; reason: string }[];
  // Deterministic input-contract breaches — a hard input/output diff with
  // separate provenance from the LLM judge: a carried-through parameter this
  // node silently rewrote. Empty/absent when none.
  contract_violations?: {
    run_id: string;
    agent: string;
    key: string;
    from: unknown;
    to: unknown;
  }[];
  // Named reproducible check results with provenance "deterministic" (never
  // the LLM judge) — signal contract in docs/deterministic-signals.md.
  deterministic_signals?: {
    name: string;
    run_id: string;
    agent: string;
    severity: string;
    // `code` + `params` are the typed fact; `detail`/`basis` are the two
    // sentences rendered from that one payload (so they cannot disagree).
    // Branch on `code`, display `detail`/`basis`. Absent on older reports.
    code?: string;
    params?: Record<string, unknown>;
    detail: string;
    basis: string;
    provenance: string;
    scope?: string;
  }[];
  // Topology classification AS RECORDED by the blame engine (the evidential
  // copy). When present the UI must prefer it over the client-side mirror in
  // web/src/topology.ts. Advisory/presentational; absent on older reports.
  topology?: TopologyClassification | null;
}

// serializers.report_summary
export interface ReportSummary {
  report_type: ReportType | null;
  culprit_run_ids: string[] | null;
  confidence: number | null;
  downstream_cost_usd: number | null;
}

// serializers.report_detail
export interface ReportDetail {
  id: number;
  incident_id: number;
  graph_id: string;
  version: number;
  is_latest: boolean;
  report_type: ReportType | null;
  culprit_run_ids: string[] | null;
  propagation_path: string[] | null;
  confidence: number | null;
  downstream_cost_usd: number | null;
  unscored_run_ids: string[] | null;
  evidence: Evidence | null;
  created_at: string;
}

// serializers.incident_summary
export interface IncidentSummary {
  id: number;
  graph_id: string;
  incident_key: string;
  trigger: IncidentTrigger;
  status: IncidentStatus;
  created_at: string;
  updated_at: string;
  latest_report: ReportSummary | null;
}

export interface IncidentListResponse {
  incidents: IncidentSummary[];
  limit: number;
  offset: number;
}

// serializers.incident_detail
export interface IncidentDetail {
  id: number;
  graph_id: string;
  incident_key: string;
  trigger: IncidentTrigger;
  status: IncidentStatus;
  created_at: string;
  updated_at: string;
  latest_report: ReportDetail | null;
}

// GET /agents/leaderboard
export interface LeaderboardAgent {
  agent_name: string | null;
  total_cost_usd: number | null;
  run_count: number | null;
  failure_rate: number | null;
  avg_quality_score: number | null;
  // Present only with ?group_by=version — rows then group by all four identity
  // fields (agent_name, agent_version, model_name, prompt_hash).
  agent_version?: string | null;
  model_name?: string | null;
  prompt_hash?: string | null;
}

export interface LeaderboardResponse {
  agents: LeaderboardAgent[];
}

// GET /graphs/{id}/version-diff?against=last_clean|<graph_id>
// The "why did it work yesterday" view: per-agent identity fields of this
// graph vs a baseline graph (the most recent OTHER finalized graph with zero
// incidents when against=last_clean).
export interface VersionIdentity {
  agent_version: string | null;
  model_name: string | null;
  prompt_hash: string | null;
  tool_schema_hash: string | null;
}

export interface VersionDiffAgent {
  agent_name: string;
  current: VersionIdentity;
  // null when the agent does not appear in the baseline graph.
  baseline: VersionIdentity | null;
  // Names of identity fields that differ from the baseline.
  changed: string[];
}

export interface VersionDiffResponse {
  graph_id: string;
  // The RESOLVED baseline graph id; null when no clean baseline graph exists.
  against: string | null;
  against_mode: "last_clean" | "explicit";
  per_agent: VersionDiffAgent[];
}

// GET /graphs/{id}/policy-decisions — shadow-mode annotations recorded after
// the fact. A decision says what a rule WOULD HAVE done; nothing was enforced.
export type PolicyDecisionKind = "would_block" | "would_warn";

export interface PolicyDecision {
  rule_name: string;
  decision: PolicyDecisionKind;
  detail: string | null;
  mode: string;
  created_at: string;
}

export interface PolicyDecisionsResponse {
  decisions: PolicyDecision[];
}

// POST /graphs/{id}/feedback — HUMAN ground truth about the RUN itself
// (label "bad" = the run truly failed), not an opinion about the report.
export type GroundTruthLabel = "ok" | "bad";

export interface FeedbackRequest {
  label: GroundTruthLabel;
  culprit_run_id?: string;
  note?: string;
}

export interface FeedbackResponse {
  id: number;
}

// GET /control/breakers — RECORDED breaker state. Agent Detective observes;
// enforcement only happens if the integration polls this via the SDK hook.
export interface BreakerRow {
  scope_kind: "agent_name" | "agent_version";
  scope_value: string;
  state: "open" | "closed";
  reason: string | null;
  opened_at: string | null;
  updated_at: string;
}

export interface BreakersResponse {
  breakers: BreakerRow[];
}
