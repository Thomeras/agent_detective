// Screen 3: the agent leaderboard.
//
// Cost and failure rate per agent_name, optionally per (agent_name,
// agent_version, model_name, prompt_hash) identity tuple — the "leaderboard per
// version" mode. Rendered as ranked records with inline bars: rates and scores
// are proportions, and a proportion is read faster as a bar than as a decimal
// buried in the fifth column of a table.

import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { LeaderboardAgent } from "../api/types";
import { EmptyState, ErrorState, Loading } from "../components/ui";
import {
  Bar,
  Field,
  Page,
  RecordFields,
  RecordList,
  RecordRow,
  SearchInput,
  Segmented,
  Select,
  StatTile,
  Toolbar,
} from "../ui/primitives";
import type { Tone } from "../verdict/descriptor";
import { formatCost, formatCoverage, formatPercent, formatScore, sumWithCoverage } from "../format";
import { useAsync } from "../hooks/useAsync";

type SortKey = "cost" | "failure" | "quality" | "runs";
type Grouping = "agent" | "version";

const SORTS: { value: SortKey; label: string }[] = [
  { value: "cost", label: "Highest cost" },
  { value: "failure", label: "Highest failure rate" },
  { value: "quality", label: "Lowest quality" },
  { value: "runs", label: "Most runs" },
];

function qualityTone(score: number | null | undefined): Tone {
  if (score == null || !Number.isFinite(score)) return "unknown";
  if (score >= 0.8) return "ok";
  if (score >= 0.5) return "warn";
  return "fail";
}

function failureTone(rate: number | null | undefined): Tone {
  if (rate == null || !Number.isFinite(rate)) return "unknown";
  if (rate === 0) return "ok";
  if (rate < 0.25) return "warn";
  return "fail";
}

function identityKey(a: LeaderboardAgent, idx: number): string {
  return `${a.agent_name ?? "unknown"}|${a.agent_version ?? ""}|${a.model_name ?? ""}|${
    a.prompt_hash ?? ""
  }|${idx}`;
}

export default function Leaderboard() {
  const [grouping, setGrouping] = useState<Grouping>("agent");
  const [sort, setSort] = useState<SortKey>("cost");
  const [q, setQ] = useState("");

  const { data, loading, error, reload } = useAsync(
    () => api.leaderboard(grouping === "version" ? "version" : undefined),
    [grouping],
  );

  const agents = useMemo(() => data?.agents ?? [], [data]);

  const totals = useMemo(() => {
    // `?? 0` here priced every uninstrumented agent at free and printed the
    // result as one confident total.
    const { total: spend, coverage: spendCoverage } = sumWithCoverage(
      agents.map((a) => ({
        cost: a.total_cost_usd,
        priced: a.priced_run_count,
        total: a.run_count,
      })),
    );
    const runs = agents.reduce((s, a) => s + (a.run_count ?? 0), 0);
    const scored = agents.filter((a) => a.avg_quality_score != null);
    const avgQuality =
      scored.length === 0
        ? null
        : scored.reduce((s, a) => s + (a.avg_quality_score ?? 0), 0) / scored.length;
    const worst = agents.reduce<LeaderboardAgent | null>(
      (acc, a) => ((a.failure_rate ?? 0) > (acc?.failure_rate ?? 0) ? a : acc),
      null,
    );
    return { spend, spendCoverage, runs, avgQuality, worst };
  }, [agents]);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const filtered = agents.filter((a) =>
      needle
        ? [a.agent_name, a.agent_version, a.model_name, a.prompt_hash]
            .filter(Boolean)
            .join(" ")
            .toLowerCase()
            .includes(needle)
        : true,
    );
    const sorted = [...filtered];
    if (sort === "cost") sorted.sort((a, b) => (b.total_cost_usd ?? 0) - (a.total_cost_usd ?? 0));
    if (sort === "failure") sorted.sort((a, b) => (b.failure_rate ?? 0) - (a.failure_rate ?? 0));
    if (sort === "runs") sorted.sort((a, b) => (b.run_count ?? 0) - (a.run_count ?? 0));
    if (sort === "quality") {
      // Unscored agents last: an absent score is not a bad score.
      sorted.sort((a, b) => (a.avg_quality_score ?? 2) - (b.avg_quality_score ?? 2));
    }
    return sorted;
  }, [agents, sort, q]);

  return (
    <Page
      title="Agents"
      subtitle={
        grouping === "version"
          ? "Per agent-version identity (version, model, prompt hash): cost, failure rate and average quality."
          : "Per-agent cost, failure rate and average quality across every graph."
      }
      actions={
        <button className="btn" onClick={reload} disabled={loading}>
          Refresh
        </button>
      }
    >
      {loading && <Loading label="Loading leaderboard" />}
      {error && <ErrorState message={error} onRetry={reload} />}

      {!loading && !error && agents.length === 0 && (
        <EmptyState
          title="No agent data yet"
          hint="Once runs are ingested, per-agent cost and failure rates show here."
        />
      )}

      {!loading && !error && agents.length > 0 && (
        <>
          <div className="stat-row">
            <StatTile label={grouping === "version" ? "Identities" : "Agents"} value={agents.length} />
            <StatTile label="Runs" value={totals.runs} />
            <StatTile
              label="Total spend"
              value={formatCost(totals.spend)}
              hint={formatCoverage(totals.spendCoverage) ?? "cost coverage not recorded"}
            />
            <StatTile
              label="Mean quality"
              value={formatScore(totals.avgQuality)}
              tone={qualityTone(totals.avgQuality)}
            />
            <StatTile
              label="Worst failure rate"
              value={formatPercent(totals.worst?.failure_rate ?? 0)}
              tone={failureTone(totals.worst?.failure_rate)}
              hint={totals.worst?.agent_name ?? undefined}
            />
          </div>

          <Toolbar>
            <SearchInput value={q} onChange={setQ} placeholder="Search agent, model, prompt hash…" />
            <Segmented<Grouping>
              value={grouping}
              onChange={setGrouping}
              options={[
                { value: "agent", label: "By agent" },
                {
                  value: "version",
                  label: "By version",
                  title: "Group by (agent, version, model, prompt hash) instead of agent alone",
                },
              ]}
            />
            <Select<SortKey> value={sort} onChange={setSort} options={SORTS} title="Sort order" />
            <div className="toolbar-end">
              {rows.length} of {agents.length} shown
            </div>
          </Toolbar>

          {rows.length === 0 ? (
            <EmptyState title="No agents match the search" />
          ) : (
            <RecordList>
              {rows.map((agent, idx) => {
                const qTone = qualityTone(agent.avg_quality_score);
                const fTone = failureTone(agent.failure_rate);
                return (
                  <RecordRow key={identityKey(agent, idx)} tone={fTone} dense>
                    <div className="rec-top">
                      <span className={`rank${idx < 3 ? " top" : ""}`}>{idx + 1}</span>
                      <span className="rec-title mono">{agent.agent_name ?? "(unknown)"}</span>
                      <span className="rec-end">
                        <span className="rec-time">
                          {agent.run_count ?? 0} run{(agent.run_count ?? 0) === 1 ? "" : "s"}
                        </span>
                      </span>
                    </div>

                    <RecordFields>
                      <Field label="Avg quality" tone={qTone}>
                        {formatScore(agent.avg_quality_score)}
                        <Bar value={agent.avg_quality_score} tone={qTone} />
                      </Field>
                      <Field label="Failure rate" tone={fTone}>
                        {formatPercent(agent.failure_rate)}
                        <Bar value={agent.failure_rate} tone={fTone} />
                      </Field>
                      <Field label="Total cost">{formatCost(agent.total_cost_usd)}</Field>
                      {grouping === "version" && (
                        <>
                          <Field label="Version" faint>
                            {agent.agent_version ?? "—"}
                          </Field>
                          <Field label="Model" faint>
                            {agent.model_name ?? "—"}
                          </Field>
                          <Field label="Prompt hash" faint wide>
                            {agent.prompt_hash ?? "—"}
                          </Field>
                        </>
                      )}
                    </RecordFields>
                  </RecordRow>
                );
              })}
            </RecordList>
          )}
        </>
      )}
    </Page>
  );
}
