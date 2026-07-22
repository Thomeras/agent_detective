// Screen 2 (spec 6.4): single execution graph.
// Cytoscape canvas on the left; a side panel on the right shows the selected
// node's details/payloads and the latest BlameReport for the incident.

import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { GraphDetail, RunNodeData, RunPayloads } from "../api/types";
import BlameReportPanel from "../components/BlameReportPanel";
import GraphCanvas from "../components/GraphCanvas";
import { EmptyState, ErrorState, Loading, Panel, StatusBadge } from "../components/ui";
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

function Legend() {
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
      <div className="legend-group">
        <span className="legend-title">Edges</span>
        <span className="legend-item">
          <span className="edge-swatch spawn" /> SPAWN
        </span>
        <span className="legend-item">
          <span className="edge-swatch a2a" /> A2A_MESSAGE
        </span>
        <span className="legend-item">
          <span className="edge-swatch tool" /> TOOL_DELEGATION
        </span>
      </div>
      <div className="legend-group">
        <span className="legend-item">
          <span className="ring-swatch" /> culprit
        </span>
        <span className="legend-item">
          <span className="path-swatch" /> propagation path
        </span>
        <span className="legend-item">
          <span className="loop-swatch" /> retry loop
        </span>
      </div>
    </div>
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

export default function GraphView({ graphId, incidentId }: { graphId: string; incidentId: number | null }) {
  const graphState = useAsync<GraphDetail>(() => api.getGraph(graphId), [graphId]);
  const incidentState = useAsync(
    () => (incidentId !== null ? api.getIncident(incidentId) : Promise.resolve(null)),
    [incidentId],
  );

  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeMsg, setAnalyzeMsg] = useState<string | null>(null);

  const graph = graphState.data;
  const report = incidentState.data?.latest_report ?? null;

  const nodeById = useMemo(() => {
    const map = new Map<string, RunNodeData>();
    graph?.nodes.forEach((n) => map.set(n.data.id, n.data));
    return map;
  }, [graph]);

  const labelFor = useMemo(
    () => (runId: string) => nodeById.get(runId)?.agent_name ?? shortId(runId),
    [nodeById],
  );

  const culprits = useMemo(() => new Set(report?.culprit_run_ids ?? []), [report]);
  const pathNodes = useMemo(() => new Set(report?.propagation_path ?? []), [report]);
  const pathEdgeKeys = useMemo(() => {
    const keys = new Set<string>();
    const path = report?.propagation_path ?? [];
    for (let i = 0; i + 1 < path.length; i++) {
      keys.add(`${path[i]}|${path[i + 1]}`);
    }
    return keys;
  }, [report]);

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
        <div className="graph-layout">
          <div className="graph-main">
            <Legend />
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
            {report ? (
              <BlameReportPanel report={report} labelFor={labelFor} onSelectRun={setSelectedRunId} />
            ) : (
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
            )}
            {incidentId === null && !selectedNode && (
              <Panel title="Details">
                <EmptyState title="Select a node" hint="Click any node to inspect its run and payloads." />
              </Panel>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
