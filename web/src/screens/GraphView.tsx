// Screen 2: one execution graph.
//
// The old layout stacked everything on one page — verdict, defect cards,
// feedback, raw JSON, legend, canvas and a cramped sticky aside — so the answer
// and the audit trail competed for the same screen. It is now four tabs behind
// one sticky verdict header:
//
//   Defects   the answer: what broke, where, how sure
//   Topology  the canvas, full width
//   Runs      every node as a record row (previously only reachable by
//             clicking a dot on the canvas)
//   Evidence  the audit trail: raw evidence, policy shadow, version diff
//
// Node details open in a slide-over drawer, so payloads get real width instead
// of a 380px column.

import { useMemo, useState } from "react";

import { api } from "../api/client";
import type {
  EdgeType,
  Evidence,
  GraphDetail,
  GroundTruthLabel,
  ReportDetail,
  ReportType,
  RunNodeData,
  RunPayloads,
  TopologyClassification,
  VersionDiffResponse,
  VersionIdentity,
} from "../api/types";
import { classifyTopology } from "../topology";
import BlameReportPanel from "../components/BlameReportPanel";
import DefectCard from "../components/DefectCard";
import GraphCanvas, { detectLoops } from "../components/GraphCanvas";
import { EmptyState, ErrorState, Loading, Panel, StatusBadge, TypeBadge } from "../components/ui";
import {
  Badge,
  Bar,
  Disclosure,
  Drawer,
  Field,
  Page,
  RecordFields,
  RecordList,
  RecordRow,
  SearchInput,
  Tabs,
  Toolbar,
} from "../ui/primitives";
import { toSchemaTwo, type SchemaTwoEvidence } from "../verdict/types";
import type { Tone } from "../verdict/descriptor";
import { defectDescriptor, originPhrase, verdictDescriptor } from "../verdict/descriptor";
import { incidentByGraph } from "../verdict/runVerdict";
import {
  channelCoverage,
  formatCost,
  formatScore,
  formatTime,
  formatUsd,
  formatWeights,
  judgeLabel,
  scoreColor,
  shortId,
} from "../format";
import { useAsync } from "../hooks/useAsync";
import { collectNodeReasons, explainUnscored } from "../nodeReasons";
import { href, useRoute } from "../router";
import { buildFindingsMarkdown, downloadText } from "../findingsExport";

type TabKey = "defects" | "topology" | "runs" | "evidence";

const TAB_KEYS: TabKey[] = ["defects", "topology", "runs", "evidence"];

// The legend is DERIVED from the loaded trace: it captions only what this graph
// actually contains. A static key advertising markers the trace does not have
// promises a graph the demo then fails to show.
function Legend({ graph, report }: { graph: GraphDetail; report: ReportDetail | null }) {
  const edgeTypes = new Set(graph.edges.map((e) => e.data.type));
  const { loopNodes } = detectLoops(
    graph.nodes.map((n) => n.data.id),
    graph.edges.map((e) => ({ source: e.data.source, target: e.data.target })),
    new Map(),
  );
  const hasCulprit = (report?.culprit_run_ids ?? []).length > 0;
  const hasPath = (report?.propagation_path ?? []).length >= 2;
  const edgeEntries: Array<[string, string]> = [
    ["SPAWN", "spawn"],
    ["A2A_MESSAGE", "a2a"],
    ["TOOL_DELEGATION", "tool"],
  ];
  return (
    <div className="legend">
      <div className="legend-group">
        <span className="legend-title">Quality</span>
        <span className="legend-item">
          <span className="swatch" style={{ background: scoreColor(0.9) }} /> good
        </span>
        <span className="legend-item">
          <span className="swatch" style={{ background: scoreColor(0.5) }} /> mid
        </span>
        <span className="legend-item">
          <span className="swatch" style={{ background: scoreColor(0.1) }} /> bad
        </span>
        <span className="legend-item">
          <span className="swatch" style={{ background: scoreColor(null) }} /> unknown
        </span>
      </div>
      {edgeTypes.size > 0 && (
        <div className="legend-group">
          <span className="legend-title">Edges</span>
          {edgeEntries
            .filter(([type]) => edgeTypes.has(type as EdgeType))
            .map(([type, cls]) => (
              <span key={type} className="legend-item">
                <span className={`edge-swatch ${cls}`} /> {type}
              </span>
            ))}
        </div>
      )}
      <div className="legend-group">
        <span className="legend-title">Node kind</span>
        <span className="legend-item">
          <span className="swatch swatch-round" /> LLM call
        </span>
        <span className="legend-item">
          <span className="swatch swatch-square" /> deterministic
        </span>
      </div>
      {(hasCulprit || hasPath || loopNodes.size > 0) && (
        <div className="legend-group">
          {hasCulprit && (
            <span className="legend-item">
              <span className="ring-swatch" /> culprit
            </span>
          )}
          {hasPath && (
            <span className="legend-item">
              <span className="path-swatch" /> propagation path
            </span>
          )}
          {loopNodes.size > 0 && (
            <span className="legend-item">
              <span className="loop-swatch" /> retry loop
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// Advisory topology chip. Prefers the EVIDENTIAL classification recorded in the
// open blame report over the client-side mirror; the tooltip names which source
// rendered. Presentational only: it never affects the report.
function TopologyChip({
  graph,
  evidenceTopology,
}: {
  graph: GraphDetail;
  evidenceTopology: TopologyClassification | null;
}) {
  const clientTopology = useMemo(
    () =>
      graph.nodes.length === 0
        ? null
        : classifyTopology(
            graph.nodes.map((n) => n.data.id),
            graph.edges.map((e) => [e.data.source, e.data.target] as [string, string]),
          ),
    [graph],
  );

  const topo = evidenceTopology ?? clientTopology;
  if (!topo) return null;
  const disconnected = topo.primary === "disconnected";

  const lines = [
    `topology: ${topo.primary} (${
      evidenceTopology ? "as recorded in evidence" : "computed client-side"
    })`,
    `nodes: ${topo.node_count}`,
    `edges: ${topo.edge_count}`,
    `components: ${topo.components}`,
    `depth: ${topo.depth}`,
    `max fan-out: ${topo.max_fan_out}`,
    `SCCs: ${topo.scc_count}`,
    `bidirectional pairs: ${topo.bidirectional_pairs}`,
  ];
  if (disconnected) {
    lines.push(
      "",
      `${topo.components} weakly-connected components: runs share graph membership but lack instrumented edges between components, so blame localisation across components is impossible. Enable A2A detection or instrument SPAWN/TOOL edges.`,
    );
  }

  return (
    <span className="topo-chip" title={lines.join("\n")}>
      <TypeBadge label={`topology: ${topo.primary}`} kind={disconnected ? "warn" : undefined} />
    </span>
  );
}

function statusTone(status: string): Tone {
  if (status === "failed") return "fail";
  if (status === "degraded") return "warn";
  if (status === "ok") return "ok";
  return "unknown";
}

// The score, not the run status: a node can finish "ok" and still score 0.27,
// and colouring its bar green would hide exactly the row worth looking at.
function scoreTone(score: number | null): Tone {
  if (score == null || !Number.isFinite(score)) return "unknown";
  if (score >= 0.8) return "ok";
  if (score >= 0.5) return "warn";
  return "fail";
}

// Map a reason severity onto the shared badge tones.
function reasonTone(severity: "fail" | "warn" | "info"): Tone {
  if (severity === "fail") return "fail";
  if (severity === "warn") return "warn";
  return "unknown";
}

// The composite is a weighted mean over the channels that REPORTED, not over a
// fixed three: an absent channel hands its weight to the rest (schema absent ->
// the judge's 0.40 becomes 0.727), and nothing said so. Coverage plus the
// EFFECTIVE weights say it. The nominal weights are never shown — this client
// does not know them, and a guessed pair would be invented provenance.
function ScoreComponents({ node }: { node: RunNodeData }) {
  const components = node.score_components ?? {};
  const coverage = channelCoverage(components);
  const partial = coverage.reported < coverage.total;
  const weights = formatWeights(node.score_weights);
  // An unscored node blended nothing, so it has no coverage to claim and no
  // weights to be missing — the unscored line above is the honest statement.
  const blended = node.quality_score != null;
  return (
    <div className="reason-block">
      <div className="blame-label">
        Score components{" "}
        {blended && (
          <Badge
            tone={partial ? "warn" : "ok"}
            title={
              partial
                ? "The composite is a weighted mean over the channels that reported — the absent channels' weight was redistributed onto these."
                : "Every scoring channel reported for this node."
            }
          >
            {coverage.reported} of {coverage.total} channels
          </Badge>
        )}
      </div>
      <div className="components">
        {Object.entries(components).map(([key, val]) => {
          const weight = node.score_weights?.[key];
          return (
            <span
              key={key}
              className="component-chip"
              title={
                weight != null
                  ? `effective weight: ${Math.round(weight * 100)}% of this composite`
                  : undefined
              }
            >
              {key}: {val === null ? "—" : val.toFixed(2)}
              {weight != null && ` · ${Math.round(weight * 100)}%`}
            </span>
          );
        })}
      </div>
      {blended && (
        <div className="muted small">
          {weights ? `effective weights: ${weights}` : "effective weights not recorded"}
        </div>
      )}
      {components.judge != null && (
        <div className="muted small">judged by {judgeLabel(node.judge_model)}</div>
      )}
    </div>
  );
}

// Node details, rendered inside the drawer.
function NodeDetails({
  node,
  graphId,
  evidence,
}: {
  node: RunNodeData;
  graphId: string;
  evidence: Evidence | null;
}) {
  const [payloads, setPayloads] = useState<RunPayloads | null>(null);
  const [loadingPayloads, setLoadingPayloads] = useState(false);
  const [payloadError, setPayloadError] = useState<string | null>(null);

  // Why this node scored what it scored, from the blame report's evidence.
  const reasons = useMemo(() => collectNodeReasons(node.id, evidence), [node.id, evidence]);
  const unscored = explainUnscored(node.unscored_reason);

  const loadPayloads = () => {
    setLoadingPayloads(true);
    setPayloadError(null);
    api
      .getRunPayloads(graphId, node.id)
      .then(setPayloads)
      .catch((err: unknown) => setPayloadError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoadingPayloads(false));
  };

  return (
    <>
      <div className="kv-grid">
        <div className="kv">
          <span className="kv-key">Quality</span>
          <span className="kv-val">
            <span className="score-chip" style={{ background: scoreColor(node.quality_score) }}>
              {formatScore(node.quality_score)}
            </span>
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">Status</span>
          <span className="kv-val">
            <StatusBadge status={node.status} />
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">Run</span>
          <span className="kv-val mono" title={node.id}>
            {shortId(node.id)}
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">Version</span>
          <span className="kv-val">{node.agent_version ?? "—"}</span>
        </div>
        <div className="kv">
          <span className="kv-key">Model</span>
          <span className="kv-val">{node.model_name ?? "—"}</span>
        </div>
        <div className="kv">
          <span className="kv-key">Prompt hash</span>
          <span className="kv-val mono">{node.prompt_hash ?? "—"}</span>
        </div>
        <div className="kv">
          <span className="kv-key">Cost</span>
          <span className="kv-val">{formatUsd(node.cost_usd)}</span>
        </div>
        <div className="kv">
          <span className="kv-key">Tokens</span>
          <span className="kv-val">
            {node.tokens_in ?? "—"} / {node.tokens_out ?? "—"}
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">Input flawed</span>
          <span className="kv-val">
            {node.input_flawed === null ? "—" : node.input_flawed ? "yes" : "no"}
          </span>
        </div>
      </div>

      {/* Muted register on purpose: a withheld measurement is not an error, and
          a deliberately skipped judge is a correct outcome. */}
      {unscored && (
        <div className="muted small">
          {unscored.deliberate ? "not scored by design" : "unscored"} — {unscored.title}
          {unscored.detail && <p className="muted small">{unscored.detail}</p>}
        </div>
      )}

      {reasons.length > 0 && (
        <div className="reason-block">
          <div className="blame-label">Why this score</div>
          <div className="reason-list">
            {reasons.map((reason, i) => (
              <div key={i} className="reason-item">
                <div className="reason-head">
                  <Badge tone={reasonTone(reason.severity)}>{reason.severity}</Badge>
                  <span className="reason-title">{reason.title}</span>
                  {/* Judge prose without its instrument is not reproducible. */}
                  {reason.kind === "judge" && (
                    <span className="muted small">{judgeLabel(node.judge_model)}</span>
                  )}
                </div>
                {reason.detail && <p className="reason-detail">{reason.detail}</p>}
                {reason.example && <pre className="reason-example">{reason.example}</pre>}
              </div>
            ))}
          </div>
        </div>
      )}

      {reasons.length === 0 && !evidence && scoreTone(node.quality_score) === "fail" && (
        <div className="muted small">
          No blame report for this graph — run Analyze to get per-node failure reasons.
        </div>
      )}

      {node.score_components && Object.keys(node.score_components).length > 0 && (
        <ScoreComponents node={node} />
      )}

      {(node.input_summary || node.output_summary) && (
        <div className="summaries">
          {node.input_summary && (
            <div>
              <div className="blame-label">Input summary</div>
              <p className="summary-text">{node.input_summary}</p>
            </div>
          )}
          {node.output_summary && (
            <div>
              <div className="blame-label">Output summary</div>
              <p className="summary-text">{node.output_summary}</p>
            </div>
          )}
        </div>
      )}

      <div className="payload-block">
        {!payloads && (
          <button className="btn" onClick={loadPayloads} disabled={loadingPayloads}>
            {loadingPayloads ? "Loading payloads…" : "Load payloads"}
          </button>
        )}
        {payloadError && <div className="state-detail error-text">{payloadError}</div>}
        {payloads && (
          <div className="payloads">
            <div>
              <div className="blame-label">Input ({payloads.input.source})</div>
              <pre className="payload-pre">{payloads.input.content ?? "(empty)"}</pre>
            </div>
            <div>
              <div className="blame-label">Output ({payloads.output.source})</div>
              <pre className="payload-pre">{payloads.output.content ?? "(empty)"}</pre>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

// The four per-run identity fields the version-diff endpoint compares.
const IDENTITY_FIELDS: { key: keyof VersionIdentity; label: string }[] = [
  { key: "agent_version", label: "version" },
  { key: "model_name", label: "model" },
  { key: "prompt_hash", label: "prompt hash" },
  { key: "tool_schema_hash", label: "tool schema" },
];

// "Why did it work yesterday": identity diff between this graph and the most
// recent clean (zero-incident) finalized graph. Fetched lazily on expand.
function VersionDiffPanel({ graphId }: { graphId: string }) {
  const [open, setOpen] = useState(false);
  const diffState = useAsync<VersionDiffResponse | null>(
    () => (open ? api.versionDiff(graphId, "last_clean") : Promise.resolve(null)),
    [graphId, open],
  );
  const diff = diffState.data;

  return (
    <Panel title="Version diff vs last clean">
      {!open ? (
        <button className="btn" onClick={() => setOpen(true)}>
          Load diff
        </button>
      ) : diffState.loading ? (
        <Loading label="Loading version diff" />
      ) : diffState.error ? (
        <ErrorState message={diffState.error} onRetry={diffState.reload} />
      ) : diff && diff.against === null ? (
        <EmptyState
          title="No clean baseline graph found"
          hint="No other finalized graph without incidents exists to diff against."
        />
      ) : diff ? (
        <>
          <div className="muted small">
            baseline: graph <span className="mono">{shortId(diff.against)}</span> (
            {diff.against_mode === "last_clean" ? "last clean" : "explicit"})
          </div>
          {diff.per_agent.length === 0 && <div className="muted small">No agents to compare.</div>}
          <div className="diff-list">
            {diff.per_agent.map((row) => {
              const changed = new Set(row.changed);
              return (
                <div key={row.agent_name} className="diff-agent">
                  <div className="diff-agent-name mono">{row.agent_name}</div>
                  {row.baseline === null && (
                    <div className="muted small">
                      not present in the baseline graph — nothing to diff against
                    </div>
                  )}
                  {IDENTITY_FIELDS.map(({ key, label }) => {
                    const isChanged = changed.has(key);
                    return (
                      <div key={key} className={`diff-field${isChanged ? " diff-changed" : ""}`}>
                        <span className="diff-key">{label}</span>
                        <span className="mono diff-val">
                          {row.baseline === null ? "—" : (row.baseline[key] ?? "—")}
                        </span>
                        <span className="score-arrow" aria-hidden>
                          →
                        </span>
                        <span className="mono diff-val">{row.current[key] ?? "—"}</span>
                        {isChanged && <TypeBadge label="changed" kind="warn" />}
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </>
      ) : null}
    </Panel>
  );
}

// Shadow-mode policy decisions. Annotations after the fact — the wording is
// always "would have blocked/warned", never "blocked".
function PolicyDecisionsPanel({ graphId }: { graphId: string }) {
  const state = useAsync(() => api.policyDecisions(graphId), [graphId]);
  const decisions = state.data?.decisions ?? [];
  if (decisions.length === 0) return null;
  return (
    <Panel title="Policy (shadow)">
      <div className="judge-list">
        {decisions.map((d, i) => (
          <div key={`${d.rule_name}-${i}`} className="fact-found">
            <TypeBadge
              label={d.decision === "would_block" ? "would block" : "would warn"}
              kind={d.decision === "would_block" ? "fail" : "warn"}
            />
            <span>
              <span className="mono">{d.rule_name}</span>: would have{" "}
              {d.decision === "would_block" ? "blocked" : "warned"}
              {d.detail ? ` — ${d.detail}` : ""}
            </span>
          </div>
        ))}
      </div>
      <p className="muted small">Shadow mode: recorded, not enforced.</p>
    </Panel>
  );
}

// Human ground truth on the RUN (not on the report). The report's verdict
// implies a run label; "correct/wrong" maps onto that. An inconclusive report
// implies nothing, so the buttons name the label directly.
function FeedbackPanel({
  graphId,
  reportType,
}: {
  graphId: string;
  reportType: ReportType | null;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState<GroundTruthLabel | null>(null);
  const [error, setError] = useState<string | null>(null);

  const implied: GroundTruthLabel | null =
    reportType === null || reportType === "unclassified"
      ? null
      : reportType === "degraded_recovered"
        ? "ok"
        : "bad";

  const submit = (label: GroundTruthLabel) => {
    setSubmitting(true);
    setError(null);
    api
      .postFeedback(graphId, { label })
      .then(() => setSubmitted(label))
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setSubmitting(false));
  };

  return (
    <Panel title="Feedback (ground truth)">
      <p className="muted small">
        Label the RUN (ground truth), not the report — "bad" means the run truly failed.
      </p>
      <div className="feedback-actions">
        {implied !== null ? (
          <>
            <button
              className="btn"
              disabled={submitting || submitted !== null}
              title={`Records: run ${implied}`}
              onClick={() => submit(implied)}
            >
              Verdict correct
            </button>
            <button
              className="btn"
              disabled={submitting || submitted !== null}
              title={`Records: run ${implied === "bad" ? "ok" : "bad"}`}
              onClick={() => submit(implied === "bad" ? "ok" : "bad")}
            >
              Verdict wrong
            </button>
          </>
        ) : (
          <>
            <button
              className="btn"
              disabled={submitting || submitted !== null}
              onClick={() => submit("ok")}
            >
              Run was ok
            </button>
            <button
              className="btn"
              disabled={submitting || submitted !== null}
              onClick={() => submit("bad")}
            >
              Run truly failed
            </button>
          </>
        )}
      </div>
      {submitted !== null && (
        <div className="muted small">
          Thanks — the run is labelled "{submitted}" as human ground truth.
        </div>
      )}
      {error && <div className="state-detail error-text">{error}</div>}
    </Panel>
  );
}

// The ANSWER, first: the verdict sentence plus a compact per-defect line, so the
// header answers "where / what kind" without scrolling to the cards. Claims
// nothing beyond each defect's own kind + origin.
function VerdictBanner({
  reportType,
  evidence,
  labelFor,
}: {
  reportType: ReportType | null;
  evidence: SchemaTwoEvidence;
  labelFor: (runId: string) => string;
}) {
  const verdict = verdictDescriptor(reportType);
  const defectLines = evidence.defects.map((d, i) => {
    const desc = defectDescriptor(d.kind, d.origin);
    return {
      key: `${d.kind}-${i}`,
      text: `${desc.label} at ${originPhrase(d.origin, labelFor)}`,
      tone: desc.tone,
    };
  });
  return (
    <div className={`verdict-banner ad-tone-${verdict.tone}`}>
      <div className="verdict-banner-head">
        <Badge tone={verdict.tone} size="lg">
          {verdict.label}
        </Badge>
        <span className="verdict-sentence">{verdict.template}</span>
      </div>
      {defectLines.length > 0 && (
        <ul className="verdict-defect-lines">
          {defectLines.map((l) => (
            <li key={l.key}>
              <span className={`verdict-dot ad-tone-${l.tone}`} aria-hidden />
              {l.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Every node of the graph as a record row — previously reachable only by
// clicking a dot on the canvas.
function RunsTab({
  graph,
  culprits,
  onSelect,
  selectedRunId,
}: {
  graph: GraphDetail;
  culprits: Set<string>;
  onSelect: (runId: string) => void;
  selectedRunId: string | null;
}) {
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const nodes = graph.nodes
    .map((n) => n.data)
    .filter((n) =>
      needle
        ? [n.agent_name, n.id, n.model_name, n.status].filter(Boolean).join(" ").toLowerCase().includes(needle)
        : true,
    );

  return (
    <>
      <Toolbar>
        <SearchInput value={q} onChange={setQ} placeholder="Search agent, run id, model…" />
        <div className="toolbar-end">
          {nodes.length} of {graph.nodes.length} runs
        </div>
      </Toolbar>
      {nodes.length === 0 ? (
        <EmptyState title="No runs match the search" />
      ) : (
        <RecordList>
          {nodes.map((node) => {
            const tone = statusTone(node.status);
            return (
              <RecordRow
                key={node.id}
                tone={tone}
                dense
                selected={selectedRunId === node.id}
                onClick={() => onSelect(node.id)}
              >
                <div className="rec-top">
                  <StatusBadge status={node.status} />
                  <span className="rec-title">
                    {node.agent_name ?? "(unnamed run)"}
                    <span className="rec-sub">{shortId(node.id)}</span>
                  </span>
                  <span className="rec-end">
                    {culprits.has(node.id) && <Badge tone="fail">blamed</Badge>}
                  </span>
                </div>
                <RecordFields>
                  <Field label="Quality" tone={scoreTone(node.quality_score)}>
                    {formatScore(node.quality_score)}
                    <Bar value={node.quality_score} tone={scoreTone(node.quality_score)} />
                  </Field>
                  <Field label="Cost">{formatUsd(node.cost_usd)}</Field>
                  <Field label="Tokens in/out">
                    {node.tokens_in ?? "—"} / {node.tokens_out ?? "—"}
                  </Field>
                  <Field label="Model" faint>
                    {node.model_name ?? "—"}
                  </Field>
                  <Field label="Version" faint>
                    {node.agent_version ?? "—"}
                  </Field>
                </RecordFields>
              </RecordRow>
            );
          })}
        </RecordList>
      )}
    </>
  );
}

export default function GraphView({
  graphId,
  incidentId,
}: {
  graphId: string;
  incidentId: number | null;
}) {
  const graphState = useAsync<GraphDetail>(() => api.getGraph(graphId), [graphId]);

  // Arriving from the runs list there is no ?incident= in the URL, but the run
  // may well have one — and its verdict is the whole point of opening it. Look
  // the live incident up by graph rather than rendering "no report".
  const incidentsState = useAsync(
    () => (incidentId === null ? api.listIncidents(200) : Promise.resolve(null)),
    [incidentId],
  );
  const resolvedIncidentId = useMemo(() => {
    if (incidentId !== null) return incidentId;
    return incidentByGraph(incidentsState.data?.incidents ?? []).get(graphId)?.id ?? null;
  }, [incidentId, incidentsState.data, graphId]);

  const incidentState = useAsync(
    () =>
      resolvedIncidentId !== null ? api.getIncident(resolvedIncidentId) : Promise.resolve(null),
    [resolvedIncidentId],
  );

  // The tab lives in the URL, so a view is linkable and survives a reload.
  const route = useRoute();
  const tabParam = route.query.get("tab");
  const tab: TabKey = TAB_KEYS.includes(tabParam as TabKey) ? (tabParam as TabKey) : "defects";
  const setTab = (next: TabKey) => {
    const q = new URLSearchParams(route.query);
    if (next === "defects") q.delete("tab");
    else q.set("tab", next);
    const qs = q.toString();
    window.location.hash = `/graphs/${graphId}${qs ? `?${qs}` : ""}`;
  };

  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedDefectIndex, setSelectedDefectIndex] = useState<number | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeMsg, setAnalyzeMsg] = useState<string | null>(null);

  const graph = graphState.data;
  const report = incidentState.data?.latest_report ?? null;

  // Legacy gate: schema-2 evidence renders through the defect-card path;
  // schema-1 / absent evidence falls back to the existing BlameReportPanel.
  const schemaTwo: SchemaTwoEvidence | null = report ? toSchemaTwo(report.evidence) : null;

  const nodeById = useMemo(() => {
    const map = new Map<string, RunNodeData>();
    graph?.nodes.forEach((n) => map.set(n.data.id, n.data));
    return map;
  }, [graph]);

  const labelFor = useMemo(
    () => (runId: string) => nodeById.get(runId)?.agent_name ?? shortId(runId),
    [nodeById],
  );

  // Canvas highlight: a selected schema-2 defect card highlights THAT defect's
  // origin + propagation; otherwise the report-level culprits/path (the legacy
  // fields are dual-written, so this works either way).
  const highlight = useMemo(() => {
    if (schemaTwo && selectedDefectIndex !== null) {
      const defect = schemaTwo.defects[selectedDefectIndex];
      if (defect) {
        const originRun = defect.origin.kind === "localized" ? [defect.origin.run_id] : [];
        return { culprits: originRun, path: [...originRun, ...defect.propagation] };
      }
    }
    return {
      culprits: report?.culprit_run_ids ?? [],
      path: report?.propagation_path ?? [],
    };
  }, [schemaTwo, selectedDefectIndex, report]);

  const culprits = useMemo(() => new Set(highlight.culprits), [highlight]);
  const pathNodes = useMemo(() => new Set(highlight.path), [highlight]);
  const pathEdgeKeys = useMemo(() => {
    const keys = new Set<string>();
    const path = highlight.path;
    for (let i = 0; i + 1 < path.length; i++) keys.add(`${path[i]}|${path[i + 1]}`);
    return keys;
  }, [highlight]);

  const selectedNode = selectedRunId ? (nodeById.get(selectedRunId) ?? null) : null;
  const verdict = verdictDescriptor(report?.report_type ?? null);

  const runAnalyze = () => {
    setAnalyzing(true);
    setAnalyzeMsg(null);
    api
      .analyzeGraph(graphId)
      .then((res) => setAnalyzeMsg(`Queued (dedup ${shortId(res.dedup_key)}). Refresh shortly.`))
      .catch((err: unknown) => setAnalyzeMsg(err instanceof Error ? err.message : String(err)))
      .finally(() => setAnalyzing(false));
  };

  const defectCount = schemaTwo?.defects.length ?? (report?.culprit_run_ids?.length ?? 0);

  return (
    <Page
      back={
        <a className="back-link" href={href("/incidents")}>
          ← Back to incidents
        </a>
      }
      title={
        <>
          <Badge tone={verdict.tone} size="lg">
            {verdict.label}
          </Badge>
          <span>{graph?.name ?? graph?.graph_type ?? "Graph"}</span>
          <span className="mono muted small">{shortId(graphId)}</span>
        </>
      }
      subtitle={
        graph && (
          <span className="meta-bar">
            <StatusBadge status={graph.status} />
            <TopologyChip graph={graph} evidenceTopology={report?.evidence?.topology ?? null} />
            <span>{graph.run_count ?? graph.nodes.length} runs</span>
            <span>{formatCost(graph.total_cost_usd)}</span>
            <span>{formatTime(graph.started_at)}</span>
          </span>
        )
      }
      actions={
        <>
          <button className="btn" onClick={graphState.reload} disabled={graphState.loading}>
            Refresh
          </button>
          <button
            className="btn"
            disabled={!graph}
            title="Download the findings as a Markdown brief for a coding agent"
            onClick={() =>
              graph &&
              downloadText(`findings-${shortId(graphId)}.md`, buildFindingsMarkdown(graph, report))
            }
          >
            Export .md
          </button>
          <button className="btn btn-primary" onClick={runAnalyze} disabled={analyzing}>
            {analyzing ? "Analyzing…" : "Re-analyze"}
          </button>
        </>
      }
    >
      {analyzeMsg && <div className="banner">{analyzeMsg}</div>}

      {graphState.loading && <Loading label="Loading graph" />}
      {graphState.error && <ErrorState message={graphState.error} onRetry={graphState.reload} />}
      {incidentState.error && (
        <ErrorState message={incidentState.error} onRetry={incidentState.reload} />
      )}

      {graph && !graphState.loading && (
        <>
          <Tabs<TabKey>
            value={tab}
            onChange={setTab}
            tabs={[
              { value: "defects", label: "Defects", count: defectCount },
              { value: "topology", label: "Topology" },
              { value: "runs", label: "Runs", count: graph.nodes.length },
              { value: "evidence", label: "Evidence" },
            ]}
          />

          {tab === "defects" && (
            <>
              {(incidentState.loading || incidentsState.loading) && (
                <Loading label="Loading incident" />
              )}

              {schemaTwo && report && (
                <>
                  <VerdictBanner
                    reportType={report.report_type}
                    evidence={schemaTwo}
                    labelFor={labelFor}
                  />
                  {schemaTwo.defects.length === 0 ? (
                    <EmptyState
                      title="No defect localised"
                      hint="Analysis ran and found nothing to blame — a clean or inconclusive verdict."
                    />
                  ) : (
                    <div className="defect-grid">
                      {schemaTwo.defects.map((defect, i) => (
                        <DefectCard
                          key={`${defect.kind}-${defect.origin.kind}-${i}`}
                          defect={defect}
                          findings={schemaTwo.findings}
                          labelFor={labelFor}
                          judgeModel={report.judge_model ?? null}
                          selected={selectedDefectIndex === i}
                          onSelect={() =>
                            setSelectedDefectIndex(selectedDefectIndex === i ? null : i)
                          }
                        />
                      ))}
                    </div>
                  )}
                  <div className="section-label">Ground truth</div>
                  <FeedbackPanel graphId={graphId} reportType={report.report_type} />
                </>
              )}

              {/* Legacy gate: schema-1 evidence keeps the original panel. */}
              {report && !schemaTwo && (
                <>
                  <BlameReportPanel
                    report={report}
                    labelFor={labelFor}
                    onSelectRun={setSelectedRunId}
                  />
                  <div className="section-label">Ground truth</div>
                  <FeedbackPanel graphId={graphId} reportType={report.report_type} />
                </>
              )}

              {!report && !incidentState.loading && !incidentsState.loading && (
                <EmptyState
                  title={
                    resolvedIncidentId === null
                      ? "No incident raised for this run"
                      : `Incident #${resolvedIncidentId} has no blame report yet`
                  }
                  hint={
                    resolvedIncidentId === null
                      ? "Nothing was flagged here. Re-analyze runs the blame engine over the graph again; the Runs and Topology tabs show the trace either way."
                      : "The worker has not produced a report for this incident yet — try Refresh in a moment."
                  }
                />
              )}
            </>
          )}

          {tab === "topology" && (
            <div className="graph-frame">
              <Legend graph={graph} report={report ?? null} />
              {graph.nodes.length === 0 ? (
                <EmptyState title="This graph has no runs yet" />
              ) : (
                <GraphCanvas
                  graph={graph}
                  culprits={culprits}
                  pathNodes={pathNodes}
                  pathEdgeKeys={pathEdgeKeys}
                  selectedRunId={selectedRunId}
                  onNodeSelect={setSelectedRunId}
                />
              )}
            </div>
          )}

          {tab === "runs" && (
            <RunsTab
              graph={graph}
              culprits={culprits}
              selectedRunId={selectedRunId}
              onSelect={setSelectedRunId}
            />
          )}

          {tab === "evidence" && (
            <div className="evidence-stack">
              {schemaTwo && (
                <Disclosure summary="Raw evidence (schema-2 JSON)">
                  <pre className="payload-pre">{JSON.stringify(schemaTwo, null, 2)}</pre>
                </Disclosure>
              )}
              {report && !schemaTwo && (
                <Panel title="Blame report (schema 1)">
                  <BlameReportPanel
                    report={report}
                    labelFor={labelFor}
                    onSelectRun={setSelectedRunId}
                  />
                </Panel>
              )}
              {!report && (
                <EmptyState
                  title="No recorded evidence"
                  hint="Nothing has been analysed for this run yet."
                />
              )}
              <PolicyDecisionsPanel graphId={graphId} />
              <VersionDiffPanel graphId={graphId} />
            </div>
          )}
        </>
      )}

      {selectedNode && (
        <Drawer
          title={
            <>
              {selectedNode.agent_name ?? "(unnamed run)"}{" "}
              <span className="mono muted small">{shortId(selectedNode.id)}</span>
            </>
          }
          onClose={() => setSelectedRunId(null)}
        >
          <NodeDetails node={selectedNode} graphId={graphId} evidence={report?.evidence ?? null} />
        </Drawer>
      )}
    </Page>
  );
}
