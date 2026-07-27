// Screen: Runs — the verdict-first list (verdict-refactor-plan.md §9.2).
//
// Replaces the old table whose Type was always "—" and Status always
// "finalized" (answering nothing). Every row now leads with the VERDICT
// (PASSED / FAILED / LATENT DEFECT / UNANALYSED / INCONCLUSIVE) plus the defect
// chips that say what broke — so "which runs are bad?" is answered at a glance
// without opening a single graph.
//
// The verdict is derived web-side (runVerdict.ts) by joining the graphs list
// with the incidents list; the server is untouched (file ownership: web/**).

import { api } from "../api/client";
import type { GraphListResponse, GraphSummary, IncidentListResponse } from "../api/types";
import { EmptyState, ErrorState, Loading } from "../components/ui";
import { Badge, Chip, Table, type Column } from "../ui/primitives";
import { incidentByGraph, runVerdictFor, type RunVerdict } from "../verdict/runVerdict";
import { formatCost, formatRelative, shortId } from "../format";
import { href } from "../router";
import { useAsync } from "../hooks/useAsync";

interface Row {
  graph: GraphSummary;
  verdict: RunVerdict;
}

function confidenceText(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

const COLUMNS: Column<Row>[] = [
  {
    key: "verdict",
    header: "Verdict",
    render: (r) => <Badge tone={r.verdict.descriptor.tone}>{r.verdict.descriptor.label}</Badge>,
  },
  {
    key: "graph",
    header: "Graph",
    render: (r) => (
      <a className="mono" href={href(`/graphs/${r.graph.graph_id}`)}>
        {r.graph.name ?? shortId(r.graph.graph_id)}
      </a>
    ),
  },
  {
    key: "defects",
    header: "Defects",
    render: (r) =>
      r.verdict.chips.length === 0 ? (
        <span className="dim">—</span>
      ) : (
        <span className="chip-row">
          {r.verdict.chips.map((c) => (
            <Chip key={c.key} tone={c.tone}>
              {c.label}
            </Chip>
          ))}
        </span>
      ),
  },
  {
    key: "confidence",
    header: "Confidence",
    numeric: true,
    render: (r) => confidenceText(r.verdict.confidence),
  },
  {
    key: "type",
    header: "Agent / type",
    render: (r) => <span className="dim">{r.graph.graph_type ?? "—"}</span>,
  },
  {
    key: "cost",
    header: "Cost",
    numeric: true,
    render: (r) => formatCost(r.graph.total_cost_usd),
  },
  {
    key: "age",
    header: "Age",
    render: (r) => (
      <span title={r.graph.started_at ?? undefined}>{formatRelative(r.graph.started_at)}</span>
    ),
  },
];

export default function GraphList() {
  const { data, loading, error, reload } = useAsync(
    () =>
      // Fetch a wide page of incidents so the verdict join covers every graph
      // on the page (an uncovered graph safely reads PASSED/UNANALYSED).
      Promise.all([api.listGraphs(100), api.listIncidents(200)]) as Promise<
        [GraphListResponse, IncidentListResponse]
      >,
    [],
  );

  const graphs = data?.[0]?.graphs ?? [];
  const incidents = data?.[1]?.incidents ?? [];
  const byGraph = incidentByGraph(incidents);
  const rows: Row[] = graphs.map((graph) => ({
    graph,
    verdict: runVerdictFor(graph, byGraph.get(graph.graph_id)),
  }));

  return (
    <div className="screen">
      <div className="screen-head">
        <div>
          <h2>Runs</h2>
          <div className="screen-sub">
            Every ingested run, verdict first. Passed, failed, latent-defect and
            unanalysed at a glance — open one to see the defect cards.
          </div>
        </div>
        <button className="btn" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      {loading && <Loading label="Loading runs" />}
      {error && <ErrorState message={error} onRetry={reload} />}
      {!loading && !error && rows.length === 0 && (
        <EmptyState
          title="No runs yet"
          hint="Point an OTEL-instrumented agent at the ingest endpoint, or run ./demo/run.sh, and finalised runs appear here."
        />
      )}

      {!loading && !error && rows.length > 0 && (
        <Table
          columns={COLUMNS}
          rows={rows}
          rowKey={(r) => r.graph.graph_id}
          onRowClick={(r) => {
            window.location.hash = `/graphs/${r.graph.graph_id}`;
          }}
        />
      )}
    </div>
  );
}
