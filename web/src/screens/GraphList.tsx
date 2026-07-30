// Screen: Runs — the verdict-first list.
//
// Every row leads with the VERDICT (PASSED / FAILED / LATENT DEFECT /
// UNANALYSED / INCONCLUSIVE) plus the defect chips that say what broke, so
// "which runs are bad?" is answered without opening a single graph.
//
// The verdict is derived web-side (runVerdict.ts) by joining the graphs list
// with the incidents list; the server is untouched.

import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { GraphListResponse, GraphSummary, IncidentListResponse } from "../api/types";
import { EmptyState, ErrorState, Loading } from "../components/ui";
import {
  Badge,
  Chip,
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
import { incidentByGraph, runVerdictFor, type RunVerdict } from "../verdict/runVerdict";
import { formatCost, formatCoverage, formatRelative, shortId, sumWithCoverage } from "../format";
import { href } from "../router";
import { useAsync } from "../hooks/useAsync";

interface Row {
  graph: GraphSummary;
  verdict: RunVerdict;
}

type ToneFilter = "all" | Tone;
type SortKey = "newest" | "oldest" | "cost" | "size";

const SORTS: { value: SortKey; label: string }[] = [
  { value: "newest", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
  { value: "cost", label: "Most expensive" },
  { value: "size", label: "Most runs" },
];

function startedMs(g: GraphSummary): number {
  const t = Date.parse(g.started_at ?? "");
  return Number.isNaN(t) ? 0 : t;
}

export default function GraphList() {
  const { data, loading, error, reload } = useAsync(
    () =>
      // A wide page of incidents so the verdict join covers every graph on the
      // page (an uncovered graph safely reads PASSED / UNANALYSED).
      Promise.all([api.listGraphs(100), api.listIncidents(200)]) as Promise<
        [GraphListResponse, IncidentListResponse]
      >,
    [],
  );

  const [toneFilter, setToneFilter] = useState<ToneFilter>("all");
  const [sort, setSort] = useState<SortKey>("newest");
  const [q, setQ] = useState("");

  const allRows: Row[] = useMemo(() => {
    const graphs = data?.[0]?.graphs ?? [];
    const byGraph = incidentByGraph(data?.[1]?.incidents ?? []);
    return graphs.map((graph) => ({
      graph,
      verdict: runVerdictFor(graph, byGraph.get(graph.graph_id)),
    }));
  }, [data]);

  const counts = useMemo(() => {
    const base: Record<ToneFilter, number> = {
      all: allRows.length,
      fail: 0,
      warn: 0,
      ok: 0,
      unknown: 0,
    };
    for (const r of allRows) base[r.verdict.descriptor.tone] += 1;
    return base;
  }, [allRows]);

  // An unpriced graph is unknown spend, not zero spend: the total carries the
  // run coverage it was actually summed over.
  const { total: spend, coverage: spendCoverage } = useMemo(
    () =>
      sumWithCoverage(
        allRows.map((r) => ({
          cost: r.graph.total_cost_usd,
          priced: r.graph.priced_run_count,
          total: r.graph.run_count,
        })),
      ),
    [allRows],
  );

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const filtered = allRows.filter((r) => {
      if (toneFilter !== "all" && r.verdict.descriptor.tone !== toneFilter) return false;
      if (!needle) return true;
      return [r.graph.graph_id, r.graph.name ?? "", r.graph.graph_type ?? "", r.verdict.descriptor.label]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
    const sorted = [...filtered];
    if (sort === "newest") sorted.sort((a, b) => startedMs(b.graph) - startedMs(a.graph));
    if (sort === "oldest") sorted.sort((a, b) => startedMs(a.graph) - startedMs(b.graph));
    if (sort === "cost") {
      sorted.sort((a, b) => (b.graph.total_cost_usd ?? -1) - (a.graph.total_cost_usd ?? -1));
    }
    if (sort === "size") {
      sorted.sort((a, b) => (b.graph.run_count ?? 0) - (a.graph.run_count ?? 0));
    }
    return sorted;
  }, [allRows, toneFilter, sort, q]);

  return (
    <Page
      title="Runs"
      subtitle="Every ingested run, verdict first. Passed, failed, latent-defect and unanalysed at a glance — open one to see the defect cards."
      actions={
        <button className="btn" onClick={reload} disabled={loading}>
          Refresh
        </button>
      }
    >
      {loading && <Loading label="Loading runs" />}
      {error && <ErrorState message={error} onRetry={reload} />}

      {!loading && !error && allRows.length === 0 && (
        <EmptyState
          title="No runs yet"
          hint="Point an OTEL-instrumented agent at the ingest endpoint, or run ./demo/run.sh, and finalised runs appear here."
        />
      )}

      {!loading && !error && allRows.length > 0 && (
        <>
          <div className="stat-row">
            <StatTile label="Runs ingested" value={counts.all} />
            <StatTile
              label="Failed"
              value={counts.fail}
              tone={counts.fail > 0 ? "fail" : undefined}
            />
            <StatTile label="With warnings" value={counts.warn} tone="warn" />
            <StatTile label="Passed" value={counts.ok} tone="ok" />
            <StatTile
              label="Total spend"
              value={formatCost(spend)}
              hint={formatCoverage(spendCoverage) ?? "cost coverage not recorded"}
            />
          </div>

          <Toolbar>
            <SearchInput value={q} onChange={setQ} placeholder="Search graph id, name, type…" />
            <Segmented<ToneFilter>
              value={toneFilter}
              onChange={setToneFilter}
              options={[
                { value: "all", label: "All", count: counts.all },
                { value: "fail", label: "Failed", count: counts.fail },
                { value: "warn", label: "Warnings", count: counts.warn },
                { value: "ok", label: "Passed", count: counts.ok },
                {
                  value: "unknown",
                  label: "Unknown",
                  count: counts.unknown,
                  title: "Unanalysed or inconclusive",
                },
              ]}
            />
            <Select<SortKey> value={sort} onChange={setSort} options={SORTS} title="Sort order" />
            <div className="toolbar-end">
              {rows.length} of {allRows.length} shown
            </div>
          </Toolbar>

          {rows.length === 0 ? (
            <EmptyState
              title="No runs match the filter"
              hint="Clear the search or pick another verdict."
            />
          ) : (
            <RecordList>
              {rows.map(({ graph, verdict }) => (
                <RecordRow
                  key={graph.graph_id}
                  tone={verdict.descriptor.tone}
                  dense
                  href={href(`/graphs/${graph.graph_id}`)}
                >
                  <div className="rec-top">
                    <Badge tone={verdict.descriptor.tone}>{verdict.descriptor.label}</Badge>
                    <span className="rec-title">
                      {graph.name ?? graph.graph_type ?? "untitled run"}
                      <span className="rec-sub">{shortId(graph.graph_id)}</span>
                    </span>
                    <span className="rec-end">
                      {verdict.chips.length > 0 && (
                        <span className="chip-row">
                          {verdict.chips.map((c) => (
                            <Chip key={c.key} tone={c.tone}>
                              {c.label}
                            </Chip>
                          ))}
                        </span>
                      )}
                      <span className="rec-time" title={graph.started_at ?? undefined}>
                        {formatRelative(graph.started_at)}
                      </span>
                    </span>
                  </div>

                  <RecordFields>
                    <Field label="Runs">{graph.run_count ?? "—"}</Field>
                    <Field label="Cost">{formatCost(graph.total_cost_usd)}</Field>
                    <Field label="Confidence">
                      {verdict.confidence != null
                        ? `${Math.round(verdict.confidence * 100)}%`
                        : "—"}
                    </Field>
                    {/* Only when it is not already the title — an untitled run
                        falls back to its type, and repeating it is noise. */}
                    {graph.name && (
                      <Field label="Type" faint>
                        {graph.graph_type ?? "—"}
                      </Field>
                    )}
                    <Field label="Status" faint>
                      {graph.status}
                    </Field>
                  </RecordFields>
                </RecordRow>
              ))}
            </RecordList>
          )}
        </>
      )}
    </Page>
  );
}
