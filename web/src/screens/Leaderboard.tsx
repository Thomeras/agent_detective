// Screen 3 (spec 6.4): agent leaderboard. Cost and failure-rate per agent_name,
// optionally grouped per (agent_name, agent_version, model_name, prompt_hash)
// identity tuple — the roadmap 2.1 "leaderboard per version" mode.

import { useState } from "react";

import { api } from "../api/client";
import { EmptyState, ErrorState, Loading } from "../components/ui";
import { formatCost, formatPercent, formatScore } from "../format";
import { useAsync } from "../hooks/useAsync";
import { scoreColor } from "../format";

export default function Leaderboard() {
  const [byVersion, setByVersion] = useState(false);
  const { data, loading, error, reload } = useAsync(
    () => api.leaderboard(byVersion ? "version" : undefined),
    [byVersion],
  );

  return (
    <div className="screen">
      <div className="screen-head">
        <div>
          <h2>Agent leaderboard</h2>
          <div className="screen-sub">
            {byVersion
              ? "Per agent-version identity (version, model, prompt hash): cost, failure rate and average quality."
              : "Per-agent cost, failure rate and average quality across every graph."}
          </div>
        </div>
        <div className="head-actions">
          <button
            className={`btn${byVersion ? " btn-primary" : ""}`}
            aria-pressed={byVersion}
            title="Group rows by (agent, version, model, prompt hash) instead of agent alone"
            onClick={() => setByVersion((v) => !v)}
          >
            Group by version
          </button>
          <button className="btn" onClick={reload} disabled={loading}>
            Refresh
          </button>
        </div>
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
                {byVersion && (
                  <>
                    <th>Version</th>
                    <th>Model</th>
                    <th>Prompt hash</th>
                  </>
                )}
                <th className="num">Runs</th>
                <th className="num">Total cost</th>
                <th className="num">Failure rate</th>
                <th className="num">Avg quality</th>
              </tr>
            </thead>
            <tbody>
              {data.agents.map((agent, idx) => (
                <tr
                  key={
                    byVersion
                      ? `${agent.agent_name}|${agent.agent_version}|${agent.model_name}|${agent.prompt_hash}|${idx}`
                      : (agent.agent_name ?? `unknown-${idx}`)
                  }
                >
                  <td className="mono">{agent.agent_name ?? "(unknown)"}</td>
                  {byVersion && (
                    <>
                      <td className="mono muted">{agent.agent_version ?? "-"}</td>
                      <td className="mono muted">{agent.model_name ?? "-"}</td>
                      <td className="mono muted">{agent.prompt_hash ?? "-"}</td>
                    </>
                  )}
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
