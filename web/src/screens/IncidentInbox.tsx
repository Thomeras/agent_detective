// Screen 1 (spec 6.4): incident inbox table.
// Columns: id, graph, trigger, suspected culprit, downstream cost, status, time.
// Rows link into the graph view for the incident's graph.

import { api } from "../api/client";
import type { IncidentSummary } from "../api/types";
import { EmptyState, ErrorState, Loading, StatusBadge, TypeBadge } from "../components/ui";
import { formatCost, formatRelative, shortId } from "../format";
import { href } from "../router";
import { useAsync } from "../hooks/useAsync";

function culpritLabel(incident: IncidentSummary): string {
  const culprits = incident.latest_report?.culprit_run_ids;
  if (!culprits || culprits.length === 0) return "-";
  if (culprits.length === 1) return shortId(culprits[0]);
  return `${shortId(culprits[0])} +${culprits.length - 1}`;
}

export default function IncidentInbox() {
  const { data, loading, error, reload } = useAsync(() => api.listIncidents(), []);

  return (
    <div className="screen">
      <div className="screen-head">
        <h2>Incident inbox</h2>
        <button className="btn" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      {loading && <Loading label="Loading incidents" />}
      {error && <ErrorState message={error} onRetry={reload} />}
      {!loading && !error && data && data.incidents.length === 0 && (
        <EmptyState
          title="No incidents yet"
          hint="Incidents appear here once the worker analyses a graph and flags degraded quality, failures, cost overruns or loops."
        />
      )}

      {!loading && !error && data && data.incidents.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Graph</th>
                <th>Trigger</th>
                <th>Report</th>
                <th>Suspected culprit</th>
                <th className="num">Downstream cost</th>
                <th>Status</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {data.incidents.map((incident) => {
                const report = incident.latest_report;
                const graphHref = href(`/graphs/${incident.graph_id}?incident=${incident.id}`);
                return (
                  <tr key={incident.id} className="row-link">
                    <td>
                      <a href={graphHref}>#{incident.id}</a>
                    </td>
                    <td className="mono">
                      <a href={graphHref}>{shortId(incident.graph_id)}</a>
                    </td>
                    <td>
                      <TypeBadge label={incident.trigger} kind={incident.trigger} />
                    </td>
                    <td>{report?.report_type ? <TypeBadge label={report.report_type} /> : "-"}</td>
                    <td className="mono">{culpritLabel(incident)}</td>
                    <td className="num">{formatCost(report?.downstream_cost_usd)}</td>
                    <td>
                      <StatusBadge status={incident.status} />
                    </td>
                    <td title={incident.created_at}>{formatRelative(incident.created_at)}</td>
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
