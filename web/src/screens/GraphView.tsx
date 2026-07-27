// Screen 2 (spec 6.4): single execution graph.
// Cytoscape canvas on the left; a side panel on the right shows the selected
// node's details/payloads and the latest BlameReport for the incident.

import { useMemo, useState } from "react";

import { api } from "../api/client";
import type {
  EdgeType,
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
import { Badge, Disclosure } from "../ui/primitives";
import { toSchemaTwo, type SchemaTwoEvidence } from "../verdict/types";
import { defectDescriptor, originPhrase, verdictDescriptor } from "../verdict/descriptor";
import {
  formatConfidence,
  formatCost,
  formatScore,
  formatTime,
  formatUsd,
  scoreColor,
  shortId,
} from "../format";
import { useAsync } from "../hooks/useAsync";
import { href } from "../router";
import { buildFindingsMarkdown, downloadText } from "../findingsExport";

// The legend is DERIVED from the loaded trace: it captions only what this
// graph actually contains. A static key advertising edge types or markers the
// trace does not have promises a graph the demo then fails to show — the
// worst place for that gap, since the graph model is the product's thesis.
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

// Advisory topology chip for the graph meta header. Prefers the EVIDENTIAL
// classification recorded in the open blame report (evidence.topology) over
// the client-side mirror computed from the loaded nodes+edges — the tooltip
// names which source rendered. Presentational only: never affects the report.
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

function NodePanel({
  node,
  graphId,
}: {
  node: RunNodeData;
  graphId: string;
}) {
  const [payloads, setPayloads] = useState<RunPayloads | null>(null);
  const [loadingPayloads, setLoadingPayloads] = useState(false);
  const [payloadError, setPayloadError] = useState<string | null>(null);

  const loadPayloads = () => {
    setLoadingPayloads(true);
    setPayloadError(null);
    api
      .getRunPayloads(graphId, node.id)
      .then(setPayloads)
      .catch((err: unknown) =>
        setPayloadError(err instanceof Error ? err.message : String(err)),
      )
      .finally(() => setLoadingPayloads(false));
  };

  return (
    <Panel
      title={
        <span className="node-panel-head">
          <span className="score-chip" style={{ background: scoreColor(node.quality_score) }}>
            {formatScore(node.quality_score)}
          </span>
          {node.agent_name ?? "(unnamed run)"}
        </span>
      }
    >
      <div className="kv-grid">
        <div className="kv">
          <span className="kv-key">Run</span>
          <span className="kv-val mono" title={node.id}>
            {shortId(node.id)}
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">Status</span>
          <span className="kv-val">
            <StatusBadge status={node.status} />
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">Version</span>
          <span className="kv-val">{node.agent_version ?? "-"}</span>
        </div>
        <div className="kv">
          <span className="kv-key">Model</span>
          <span className="kv-val">{node.model_name ?? "-"}</span>
        </div>
        <div className="kv">
          <span className="kv-key">Prompt hash</span>
          <span className="kv-val mono">{node.prompt_hash ?? "-"}</span>
        </div>
        <div className="kv">
          <span className="kv-key">Cost</span>
          <span className="kv-val">{formatUsd(node.cost_usd)}</span>
        </div>
        <div className="kv">
          <span className="kv-key">Tokens</span>
          <span className="kv-val">
            {node.tokens_in ?? "-"} / {node.tokens_out ?? "-"}
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">Input flawed</span>
          <span className="kv-val">
            {node.input_flawed === null ? "-" : node.input_flawed ? "yes" : "no"}
          </span>
        </div>
      </div>

      {node.unscored_reason && (
        <div className="muted small">unscored: {node.unscored_reason}</div>
      )}

      {node.score_components && Object.keys(node.score_components).length > 0 && (
        <div className="components">
          {Object.entries(node.score_components).map(([key, val]) => (
            <span key={key} className="component-chip">
              {key}: {val === null ? "-" : val.toFixed(2)}
            </span>
          ))}
        </div>
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
            {loadingPayloads ? "Loading payloads..." : "Load payloads"}
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
    </Panel>
  );
}

// The four per-run identity fields the version-diff endpoint compares
// (roadmap 2.1 — "why did it work yesterday").
const IDENTITY_FIELDS: { key: keyof VersionIdentity; label: string }[] = [
  { key: "agent_version", label: "version" },
  { key: "model_name", label: "model" },
  { key: "prompt_hash", label: "prompt hash" },
  { key: "tool_schema_hash", label: "tool schema" },
];

// Collapsible "why did it work yesterday" panel: identity diff between this
// graph and the most recent clean (zero-incident) finalized graph. Fetched
// lazily — the request only fires once the user expands the panel.
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
          {diff.per_agent.length === 0 && (
            <div className="muted small">No agents to compare.</div>
          )}
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
                      <div
                        key={key}
                        className={`diff-field${isChanged ? " diff-changed" : ""}`}
                      >
                        <span className="diff-key">{label}</span>
                        <span className="mono diff-val">
                          {row.baseline === null
                            ? "—"
                            : (row.baseline[key] ?? "-")}
                        </span>
                        <span className="score-arrow" aria-hidden>
                          →
                        </span>
                        <span className="mono diff-val">{row.current[key] ?? "-"}</span>
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

// Shadow-mode policy decisions recorded for this graph. Rendered only when
// rows exist. Honesty rule: these are annotations after the fact — the wording
// is always "would have blocked/warned", never "blocked".
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

// Human ground-truth feedback on the RUN (not on the report). The report's
// verdict implies a run label (a failing verdict type implies the run was bad;
// degraded_recovered implies it passed); "correct/wrong" maps onto that. An
// inconclusive report implies nothing, so the buttons name the label directly.
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
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : String(err)),
      )
      .finally(() => setSubmitting(false));
  };

  return (
    <Panel title="Feedback (ground truth)">
      <p className="muted small">
        Label the RUN (ground truth), not the report — "bad" means the run truly
        failed.
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
          // Inconclusive report: no implied label to agree/disagree with, so
          // ask for the run's ground truth directly.
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

// The ANSWER, first (§9.2): the verdict badge + the projected verdict sentence,
// derived from the typed report_type — never from a note string.
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
  // A compact per-defect summary line, so the header answers "where / what kind"
  // without scrolling to the cards. Claims nothing beyond each defect's own
  // kind + origin (§2.4).
  const defectLines = evidence.defects.map((d, i) => {
    const desc = defectDescriptor(d.kind, d.origin);
    const where = originPhrase(d.origin, labelFor);
    return {
      key: `${d.kind}-${i}`,
      text: `${desc.label} at ${where}`,
      tone: desc.tone,
    };
  });
  return (
    <div className={`verdict-banner ad-tone-${verdict.tone}`}>
      <div className="verdict-banner-head">
        <Badge tone={verdict.tone}>{verdict.label}</Badge>
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

// The primary object of the screen (§9.2): one DefectCard per Defect. Selecting
// a card lifts its index so the container can highlight that defect's path on
// the canvas.
function DefectCardsSection({
  evidence,
  labelFor,
  selectedDefectIndex,
  onSelectDefect,
}: {
  evidence: SchemaTwoEvidence;
  labelFor: (runId: string) => string;
  selectedDefectIndex: number | null;
  onSelectDefect: (index: number | null) => void;
}) {
  if (evidence.defects.length === 0) {
    return (
      <EmptyState
        title="No defect localised"
        hint="Analysis ran and found nothing to blame — a clean or inconclusive verdict."
      />
    );
  }
  return (
    <div className="defect-grid">
      {evidence.defects.map((defect, i) => (
        <DefectCard
          key={`${defect.kind}-${defect.origin.kind}-${i}`}
          defect={defect}
          findings={evidence.findings}
          labelFor={labelFor}
          selected={selectedDefectIndex === i}
          onSelect={() => onSelectDefect(selectedDefectIndex === i ? null : i)}
        />
      ))}
    </div>
  );
}

export default function GraphView({ graphId, incidentId }: { graphId: string; incidentId: number | null }) {
  const graphState = useAsync<GraphDetail>(() => api.getGraph(graphId), [graphId]);
  const incidentState = useAsync(
    () => (incidentId !== null ? api.getIncident(incidentId) : Promise.resolve(null)),
    [incidentId],
  );

  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedDefectIndex, setSelectedDefectIndex] = useState<number | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeMsg, setAnalyzeMsg] = useState<string | null>(null);

  const graph = graphState.data;
  const report = incidentState.data?.latest_report ?? null;

  // Legacy gate (§2.5): schema-2 evidence renders through the new defect-card
  // path; schema-1 / absent evidence falls back to the existing BlameReportPanel.
  const schemaTwo: SchemaTwoEvidence | null = report
    ? toSchemaTwo(report.evidence)
    : null;

  const nodeById = useMemo(() => {
    const map = new Map<string, RunNodeData>();
    graph?.nodes.forEach((n) => map.set(n.data.id, n.data));
    return map;
  }, [graph]);

  const labelFor = useMemo(
    () => (runId: string) => nodeById.get(runId)?.agent_name ?? shortId(runId),
    [nodeById],
  );

  // Canvas highlight: when a schema-2 defect card is selected, highlight THAT
  // defect's origin + propagation path; otherwise fall back to the report-level
  // culprits/path (the legacy fields are dual-written, so this works either way).
  const highlight = useMemo(() => {
    if (schemaTwo && selectedDefectIndex !== null) {
      const defect = schemaTwo.defects[selectedDefectIndex];
      if (defect) {
        const originRun =
          defect.origin.kind === "localized" ? [defect.origin.run_id] : [];
        const path = [...originRun, ...defect.propagation];
        return { culprits: originRun, path };
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
    for (let i = 0; i + 1 < path.length; i++) {
      keys.add(`${path[i]}|${path[i + 1]}`);
    }
    return keys;
  }, [highlight]);

  const selectedNode = selectedRunId ? nodeById.get(selectedRunId) ?? null : null;

  const runAnalyze = () => {
    setAnalyzing(true);
    setAnalyzeMsg(null);
    api
      .analyzeGraph(graphId)
      .then((res) => setAnalyzeMsg(`Queued (dedup ${shortId(res.dedup_key)}). Refresh shortly.`))
      .catch((err: unknown) =>
        setAnalyzeMsg(err instanceof Error ? err.message : String(err)),
      )
      .finally(() => setAnalyzing(false));
  };

  return (
    <div className="screen graph-screen">
      <div className="screen-head">
        <div>
          <a className="back-link" href={href("/incidents")}>
            back to inbox
          </a>
          <h2>
            {graph?.name ?? "Graph"} <span className="mono dim">{shortId(graphId)}</span>
          </h2>
          {graph && (
            <div className="graph-meta">
              <StatusBadge status={graph.status} />
              <TopologyChip
                graph={graph}
                evidenceTopology={report?.evidence?.topology ?? null}
              />
              <span className="dim">{graph.graph_type ?? "unknown type"}</span>
              <span className="dim">{graph.run_count ?? graph.nodes.length} runs</span>
              <span className="dim">{formatCost(graph.total_cost_usd)}</span>
              <span className="dim">{formatTime(graph.started_at)}</span>
            </div>
          )}
        </div>
        <div className="head-actions">
          <button className="btn" onClick={graphState.reload} disabled={graphState.loading}>
            Refresh
          </button>
          <button
            className="btn"
            disabled={!graph}
            title="Download the findings as a Markdown brief for a coding agent"
            onClick={() =>
              graph &&
              downloadText(
                `findings-${shortId(graphId)}.md`,
                buildFindingsMarkdown(graph, report),
              )
            }
          >
            Export .md
          </button>
          <button className="btn btn-primary" onClick={runAnalyze} disabled={analyzing}>
            {analyzing ? "Analyzing..." : "Re-analyze"}
          </button>
        </div>
      </div>
      {analyzeMsg && <div className="banner">{analyzeMsg}</div>}

      {graphState.loading && <Loading label="Loading graph" />}
      {graphState.error && <ErrorState message={graphState.error} onRetry={graphState.reload} />}

      {graph && !graphState.loading && (
        <>
          {/* Answer first (§9.2): the verdict + defect cards lead; the canvas is
              demoted to a supporting visual below. Schema-2 only — legacy
              reports keep the old BlameReportPanel in the aside. */}
          {schemaTwo && report && (
            <div className="verdict-region">
              <VerdictBanner
                reportType={report.report_type}
                evidence={schemaTwo}
                labelFor={labelFor}
              />
              <DefectCardsSection
                evidence={schemaTwo}
                labelFor={labelFor}
                selectedDefectIndex={selectedDefectIndex}
                onSelectDefect={setSelectedDefectIndex}
              />
              <FeedbackPanel graphId={graphId} reportType={report.report_type} />
              <Disclosure summary="Raw evidence (schema-2 JSON)">
                <pre className="payload-pre">{JSON.stringify(schemaTwo, null, 2)}</pre>
              </Disclosure>
            </div>
          )}

          <div className="graph-layout">
            <div className="graph-main">
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

            <aside className="graph-side">
              {selectedNode && <NodePanel node={selectedNode} graphId={graphId} />}

              {incidentState.loading && <Loading label="Loading incident" />}
              {incidentState.error && (
                <ErrorState message={incidentState.error} onRetry={incidentState.reload} />
              )}

              {/* Legacy gate: schema-1 / absent evidence renders through the
                  existing panel; schema-2 is handled by the cards above. */}
              {report && !schemaTwo ? (
                <>
                  <BlameReportPanel
                    report={report}
                    labelFor={labelFor}
                    onSelectRun={setSelectedRunId}
                  />
                  {/* Feedback lives in the container, not BlameReportPanel —
                      the report panel stays presentational. */}
                  <FeedbackPanel graphId={graphId} reportType={report.report_type} />
                </>
              ) : !report ? (
                incidentId !== null &&
                !incidentState.loading && (
                  <Panel title="Blame report">
                    <EmptyState
                      title="No blame report"
                      hint={`Incident #${incidentId} has no latest blame report yet. Confidence: ${formatConfidence(
                        null,
                      )}`}
                    />
                  </Panel>
                )
              ) : null}
              {incidentId === null && !selectedNode && (
                <Panel title="Details">
                  <EmptyState
                    title="Select a node"
                    hint="Click any node to inspect its run and payloads."
                  />
                </Panel>
              )}
              <PolicyDecisionsPanel graphId={graphId} />
              <VersionDiffPanel graphId={graphId} />
            </aside>
          </div>
        </>
      )}
    </div>
  );
}
