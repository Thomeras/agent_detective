// TypeScript mirrors of the JSON returned by services/api.
// Field names and nesting match services/api/api/serializers.py exactly.
// UUIDs, Decimals and datetimes are serialized to strings/numbers by
// serializers.json_value, so they surface here as `string | number`.

export type GraphStatus = "active" | "finalized";
export type RunStatus = "ok" | "degraded" | "failed";
export type EdgeType = "SPAWN" | "A2A_MESSAGE" | "TOOL_DELEGATION";
export type IncidentStatus = "open" | "acknowledged" | "resolved";
export type IncidentTrigger =
  | "terminal_failure"
  | "degraded_quality"
  | "cost_overrun"
  | "loop_detected"
  | "manual";

export type ReportType =
  | "cut_point"
  | "multi_culprit"
  | "composition_failure"
  | "loop_detected"
  | "root_cause_external"
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
}

export interface LeaderboardResponse {
  agents: LeaderboardAgent[];
}
