// Screen 1: the incident inbox.
//
// Rebuilt as a filterable record list. The old table pushed the columns that
// actually matter (culprit, downstream cost) off the right edge; here every
// incident is one row whose fields carry their own labels and wrap, and the
// summary tiles + filters answer "how bad is it right now" before any scrolling.

import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { IncidentSummary, IncidentStatus } from "../api/types";
import { EmptyState, ErrorState, Loading, StatusBadge } from "../components/ui";
import {
  Badge,
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
import { verdictDescriptor } from "../verdict/descriptor";
import { formatCost, formatCoverage, formatRelative, shortId } from "../format";
import { href } from "../router";
import { useAsync } from "../hooks/useAsync";

const TRIGGER_LABELS: Record<string, string> = {
  terminal_failure: "Terminal failure",
  degraded_quality: "Degraded quality",
  cost_overrun: "Cost overrun",
  loop_detected: "Loop detected",
  latent_defect: "Latent defect shipped",
  manual: "Manual",
};

type StatusFilter = "all" | "open" | "acknowledged" | "closed";
type SortKey = "newest" | "oldest" | "cost";

const SORTS: { value: SortKey; label: string }[] = [
  { value: "newest", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
  { value: "cost", label: "Highest downstream cost" },
];

function culpritLabel(incident: IncidentSummary): string {
  const culprits = incident.latest_report?.culprit_run_ids;
  if (!culprits || culprits.length === 0) return "—";
  if (culprits.length === 1) return shortId(culprits[0]);
  return `${shortId(culprits[0])} +${culprits.length - 1}`;
}

function matchesStatus(status: IncidentStatus, filter: StatusFilter): boolean {
  if (filter === "all") return true;
  if (filter === "closed") return status === "resolved" || status === "superseded";
  return status === filter;
}

function createdMs(i: IncidentSummary): number {
  const t = Date.parse(i.created_at ?? "");
  return Number.isNaN(t) ? 0 : t;
}

// Recorded circuit-breaker state. Agent Detective observes and cannot stop an
// agent unless the integration polls this via the SDK opt-in hook — the wording
// stays "recorded", never "blocked".
function BreakersStrip() {
  const { data } = useAsync(() => api.breakers(), []);
  const breakers = data?.breakers ?? [];
  if (breakers.length === 0) return null;
  return (
    <>
      <div className="section-label">Circuit breakers — recorded, not enforced</div>
      <div className="breaker-list">
        {breakers.map((b) => (
          <div key={`${b.scope_kind}:${b.scope_value}`} className="breaker-row">
            <span className={`badge badge-status-${b.state === "open" ? "open" : "resolved"}`}>
              {b.state}
            </span>
            <span className="mono">{b.scope_value}</span>
            <span className="muted small">
              ({b.scope_kind}){b.reason ? ` — ${b.reason}` : ""}
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

export default function IncidentInbox() {
  const { data, loading, error, reload } = useAsync(() => api.listIncidents(200), []);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [sort, setSort] = useState<SortKey>("newest");
  const [q, setQ] = useState("");

  const incidents = useMemo(() => data?.incidents ?? [], [data]);

  const counts = useMemo(
    () => ({
      all: incidents.length,
      open: incidents.filter((i) => i.status === "open").length,
      acknowledged: incidents.filter((i) => i.status === "acknowledged").length,
      closed: incidents.filter((i) => i.status === "resolved" || i.status === "superseded").length,
    }),
    [incidents],
  );

  // The old `?? 0` made an unpriced incident free and printed the sum as one
  // confident price. Coverage is carried instead: the total is the lower bound
  // over the incidents (and runs) that actually carried a price.
  const blastRadius = useMemo(() => {
    let total = 0;
    let pricedIncidents = 0;
    let pricedRuns = 0;
    let coveredRuns = 0;
    for (const i of incidents) {
      const cost = i.latest_report?.downstream_cost_usd;
      if (cost != null) {
        total += cost;
        pricedIncidents += 1;
      }
      const coverage = i.latest_report?.cost_coverage;
      if (coverage) {
        pricedRuns += coverage.priced;
        coveredRuns += coverage.total;
      }
    }
    // Only a fully priced set of incidents with full run coverage is a price;
    // everything else — including unknown coverage — stays a lower bound.
    const complete =
      pricedIncidents === incidents.length && coveredRuns > 0 && pricedRuns === coveredRuns;
    return { total, pricedIncidents, pricedRuns, coveredRuns, complete };
  }, [incidents]);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const filtered = incidents.filter((i) => {
      if (!matchesStatus(i.status, statusFilter)) return false;
      if (!needle) return true;
      const haystack = [
        String(i.id),
        i.graph_id,
        i.trigger,
        i.latest_report?.report_type ?? "",
        ...(i.latest_report?.culprit_run_ids ?? []),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
    const sorted = [...filtered];
    if (sort === "newest") sorted.sort((a, b) => createdMs(b) - createdMs(a));
    if (sort === "oldest") sorted.sort((a, b) => createdMs(a) - createdMs(b));
    if (sort === "cost") {
      sorted.sort(
        (a, b) =>
          (b.latest_report?.downstream_cost_usd ?? -1) -
          (a.latest_report?.downstream_cost_usd ?? -1),
      );
    }
    return sorted;
  }, [incidents, statusFilter, sort, q]);

  return (
    <Page
      title="Incidents"
      subtitle="Graphs the worker flagged for degraded quality, failure, cost overrun or a runaway loop — verdict and blamed node first."
      actions={
        <button className="btn" onClick={reload} disabled={loading}>
          Refresh
        </button>
      }
    >
      {loading && <Loading label="Loading incidents" />}
      {error && <ErrorState message={error} onRetry={reload} />}

      {!loading && !error && incidents.length === 0 && (
        <EmptyState
          title="No incidents — all clear"
          hint={
            <>
              Nothing has been flagged yet. Browse every ingested run under{" "}
              <a href={href("/graphs")}>Runs</a>, or run{" "}
              <span className="mono">./demo/inject_fault.sh &amp;&amp; ./demo/run.sh</span> to
              trigger a cut-point incident.
            </>
          }
        />
      )}

      {!loading && !error && incidents.length > 0 && (
        <>
          <div className="stat-row">
            <StatTile
              label="Open"
              value={counts.open}
              tone={counts.open > 0 ? "fail" : "ok"}
              hint="waiting on a human"
            />
            <StatTile label="Acknowledged" value={counts.acknowledged} tone="warn" />
            <StatTile label="Closed" value={counts.closed} hint="resolved or superseded" />
            <StatTile
              label="Downstream cost"
              value={
                blastRadius.pricedIncidents === 0 ? (
                  "—"
                ) : (
                  <>
                    {formatCost(blastRadius.total)}
                    {!blastRadius.complete && <span className="muted small"> lower bound</span>}
                  </>
                )
              }
              hint={
                blastRadius.pricedIncidents === 0
                  ? "no incident carries a price — nothing to total"
                  : `spend attributed to blamed work · ${blastRadius.pricedIncidents} of ${
                      incidents.length
                    } incidents priced${
                      blastRadius.coveredRuns > 0
                        ? ` · ${blastRadius.pricedRuns}/${blastRadius.coveredRuns} runs`
                        : ""
                    } — unpriced work is unknown spend, not free`
              }
            />
          </div>

          <Toolbar>
            <SearchInput
              value={q}
              onChange={setQ}
              placeholder="Search id, graph, trigger, culprit…"
            />
            <Segmented<StatusFilter>
              value={statusFilter}
              onChange={setStatusFilter}
              options={[
                { value: "all", label: "All", count: counts.all },
                { value: "open", label: "Open", count: counts.open },
                { value: "acknowledged", label: "Ack", count: counts.acknowledged },
                { value: "closed", label: "Closed", count: counts.closed },
              ]}
            />
            <Select<SortKey> value={sort} onChange={setSort} options={SORTS} title="Sort order" />
            <div className="toolbar-end">
              {rows.length} of {incidents.length} shown
            </div>
          </Toolbar>

          {rows.length === 0 ? (
            <EmptyState title="No incidents match the filter" hint="Clear the search or pick another status." />
          ) : (
            <RecordList>
              {rows.map((incident) => {
                const report = incident.latest_report;
                const verdict = verdictDescriptor(report?.report_type ?? null);
                return (
                  <RecordRow
                    key={incident.id}
                    tone={verdict.tone}
                    dense
                    href={href(`/graphs/${incident.graph_id}?incident=${incident.id}`)}
                  >
                    <div className="rec-top">
                      <Badge tone={verdict.tone}>{verdict.label}</Badge>
                      {/* The verdict sentence is identical for every incident of
                          a given report type — it belongs on the detail page,
                          not repeated down the list. */}
                      <span className="rec-title" title={verdict.template}>
                        {TRIGGER_LABELS[incident.trigger] ?? incident.trigger}
                        <span className="rec-sub">#{incident.id}</span>
                      </span>
                      <span className="rec-end">
                        <StatusBadge status={incident.status} />
                        <span className="rec-time" title={incident.created_at}>
                          {formatRelative(incident.created_at)}
                        </span>
                      </span>
                    </div>

                    <RecordFields>
                      <Field label="Graph" title={incident.graph_id}>
                        {shortId(incident.graph_id)}
                      </Field>
                      <Field label="Report">{report?.report_type ?? "—"}</Field>
                      {/* degraded_recovered points at a FRAGILE node, not a
                          culprit — the blame-neutral label covers both. */}
                      <Field label="Blamed / fragile">{culpritLabel(incident)}</Field>
                      <Field label="Confidence">
                        {report?.confidence != null
                          ? `${Math.round(report.confidence * 100)}%`
                          : "—"}
                      </Field>
                      {/* Coverage travels with the number: "6/28 runs" says how
                          much of the blast radius carried a price at all. */}
                      <Field
                        label="Downstream cost"
                        title={formatCoverage(report?.cost_coverage) ?? "cost coverage not recorded"}
                      >
                        {formatCost(report?.downstream_cost_usd)}
                        {formatCoverage(report?.cost_coverage) && (
                          <span className="muted small">
                            {" · "}
                            {formatCoverage(report?.cost_coverage)}
                          </span>
                        )}
                      </Field>
                    </RecordFields>
                  </RecordRow>
                );
              })}
            </RecordList>
          )}

          <BreakersStrip />
        </>
      )}
    </Page>
  );
}
