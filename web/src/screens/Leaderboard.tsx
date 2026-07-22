// Screen 3 (spec 6.4): agent leaderboard. Cost and failure-rate per agent_name.

import { api } from "../api/client";
import { EmptyState, ErrorState, Loading } from "../components/ui";
import { formatCost, formatPercent, formatScore } from "../format";
import { useAsync } from "../hooks/useAsync";
import { scoreColor } from "../format";

export default function Leaderboard() {
  const { data, loading, error, reload } = useAsync(() => api.leaderboard(), []);

  return (
    <div className="screen">
      <div className="screen-head">
        <div>
          <h2>Agent leaderboard</h2>
          <div className="screen-sub">
            Per-agent cost, failure rate and average quality across every graph.
          </div>
        </div>
        <button className="btn" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      {loading && <Loading label="Loading leaderboard" />}
      {error && <ErrorState message={error} onRetry={reload} />}
      {!loading && !error && data && data.agents.length === 0 && (
        <EmptyState title="No agent data yet" hint="Once runs are ingested, per-agent cost and failure rates show here." />
      )}

      {!loading && !error && data && data.agents.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Agent</th>
                <th className="num">Runs</th>
                <th className="num">Total cost</th>
                <th className="num">Failure rate</th>
                <th className="num">Avg quality</th>
              </tr>
            </thead>
            <tbody>
              {data.agents.map((agent, idx) => (
                <tr key={agent.agent_name ?? `unknown-${idx}`}>
                  <td className="mono">{agent.agent_name ?? "(unknown)"}</td>
                  <td className="num">{agent.run_count ?? 0}</td>
                  <td className="num">{formatCost(agent.total_cost_usd)}</td>
                  <td className="num">
                    <span
                      className="fail-rate"
                      style={{
                        color:
                          (agent.failure_rate ?? 0) > 0 ? "var(--danger)" : "var(--text-dim)",
                      }}
                    >
                      {formatPercent(agent.failure_rate)}
                    </span>
                  </td>
                  <td className="num">
                    <span className="score-chip" style={{ background: scoreColor(agent.avg_quality_score) }}>
                      {formatScore(agent.avg_quality_score)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
