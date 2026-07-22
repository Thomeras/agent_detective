// Screen: all execution graphs (not only ones with incidents).
// Without this, a clean run — a graph the worker finalised with no incident —
// has no route in the UI at all. Rows link into the graph view.

import { api } from "../api/client";
import type { GraphSummary } from "../api/types";
import { EmptyState, ErrorState, Loading, StatusBadge } from "../components/ui";
import { formatCost, formatRelative, shortId } from "../format";
import { href } from "../router";
import { useAsync } from "../hooks/useAsync";

export default function GraphList() {
  const { data, loading, error, reload } = useAsync(() => api.listGraphs(), []);

  return (
    <div className="screen">
      <div className="screen-head">
        <div>
          <h2>Execution graphs</h2>
          <div className="screen-sub">
            Every agent run ingested, incident or not. Open one to inspect its
            nodes, quality scores and payloads.
          </div>
        </div>
        <button className="btn" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      {loading && <Loading label="Loading graphs" />}
      {error && <ErrorState message={error} onRetry={reload} />}
      {!loading && !error && data && data.graphs.length === 0 && (
        <EmptyState
          title="No graphs yet"
          hint="Point an OTEL-instrumented agent at the ingest endpoint, or run ./demo/run.sh, and finalised graphs appear here."
        />
      )}

      {!loading && !error && data && data.graphs.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Graph</th>
                <th>Type</th>
                <th>Status</th>
                <th className="num">Runs</th>
                <th className="num">Cost</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {data.graphs.map((graph: GraphSummary) => {
                const graphHref = href(`/graphs/${graph.graph_id}`);
                return (
                  <tr key={graph.graph_id} className="row-link">
                    <td className="mono">
                      <a href={graphHref}>{graph.name ?? shortId(graph.graph_id)}</a>
                    </td>
                    <td className="dim">{graph.graph_type ?? "—"}</td>
                    <td>
                      <StatusBadge status={graph.status} />
                    </td>
                    <td className="num">{graph.run_count ?? "—"}</td>
                    <td className="num">{formatCost(graph.total_cost_usd)}</td>
                    <td title={graph.started_at ?? undefined}>
                      {formatRelative(graph.started_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
