// Renders the latest BlameReport for an incident (spec 6.4 side panel):
// report_type, culprit(s), confidence, and the evidence bundle -- score map,
// drops, judge reasoning, fact propagation, unscored nodes, downstream cost.

import type { ReportDetail } from "../api/types";
import { formatConfidence, formatCost, formatScore, shortId } from "../format";
import { scoreColor } from "../format";
import { Panel, TypeBadge } from "./ui";

interface BlameReportPanelProps {
  report: ReportDetail;
  labelFor: (runId: string) => string;
  onSelectRun: (runId: string) => void;
}

function RunRef({
  runId,
  labelFor,
  onSelectRun,
}: {
  runId: string;
  labelFor: (runId: string) => string;
  onSelectRun: (runId: string) => void;
}) {
  return (
    <button className="run-ref" onClick={() => onSelectRun(runId)} title={runId}>
      {labelFor(runId)} <span className="run-ref-id">{shortId(runId)}</span>
    </button>
  );
}

// A fallback verdict points at a *suspect*, not a proven culprit. Reflect that
// in the wording so the UI never oversells certainty (see confidence caps in
// blame_engine/blame.py).
function culpritHeading(reportType: string | null, plural: boolean): string {
  switch (reportType) {
    case "composition_failure":
      return "Most likely: orchestration / design issue";
    case "root_cause_external":
      return "Upstream / external cause";
    case "verification_gap":
      return plural ? "Rubber-stamping verifiers" : "Rubber-stamping verifier";
    case "cut_point":
      return "Origin — where quality broke";
    case "unclassified":
      return plural ? "Possible suspects" : "Possible suspect";
    default:
      return plural ? "Suspected culprits" : "Suspected culprit";
  }
}

const FALLBACK_NOTE: Record<string, string> = {
  composition_failure:
    "No single node broke — the fault could not be localised. This is a suspect, not a proven culprit.",
  root_cause_external:
    "The fault originated outside the observed graph (the input was already flawed).",
  verification_gap:
    "These verifiers reported the work healthy while the final output was bad — they let it pass unflagged.",
};

export default function BlameReportPanel({ report, labelFor, onSelectRun }: BlameReportPanelProps) {
  const evidence = report.evidence;
  const culprits = report.culprit_run_ids ?? [];
  const unscored = report.unscored_run_ids ?? [];
  const fallbackNote = report.report_type ? FALLBACK_NOTE[report.report_type] : undefined;
  const manifestation = evidence?.manifestation_run_ids ?? [];
  const verificationGaps = evidence?.verification_gaps ?? [];

  const scoreEntries = evidence ? Object.entries(evidence.score_map) : [];
  const dropEntries = evidence ? Object.entries(evidence.drops) : [];
  const judgeEntries = evidence ? Object.entries(evidence.judge_notes) : [];

  return (
    <div className="blame-panel">
      <Panel
        title={
          <span className="blame-head">
            <TypeBadge label={report.report_type ?? "unclassified"} />
            <span className="blame-version">v{report.version}</span>
          </span>
        }
      >
        <div className="kv-grid">
          <div className="kv">
            <span className="kv-key">Confidence</span>
            <span className="kv-val">{formatConfidence(report.confidence)}</span>
          </div>
          <div className="kv">
            <span className="kv-key">Downstream cost</span>
            <span className="kv-val">{formatCost(report.downstream_cost_usd)}</span>
          </div>
        </div>

        <div className="blame-section">
          <div className="blame-label">
            {culpritHeading(report.report_type, culprits.length > 1)}
          </div>
          {culprits.length === 0 ? (
            <span className="muted">none</span>
          ) : (
            <div className={`ref-list${fallbackNote ? " ref-list-suspect" : ""}`}>
              {culprits.map((runId) => (
                <RunRef key={runId} runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
              ))}
            </div>
          )}
          {fallbackNote && <p className="suspect-note">{fallbackNote}</p>}
        </div>

        {manifestation.length > 0 && (
          <div className="blame-section">
            <div className="blame-label" title="Where the failure surfaced — the terminal output — as opposed to where it broke.">
              Manifested at
            </div>
            <div className="ref-list">
              {manifestation.map((runId) => (
                <RunRef key={runId} runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
              ))}
            </div>
          </div>
        )}
      </Panel>

      {verificationGaps.length > 0 && (
        <Panel title="Verification gaps">
          <p className="suspect-note">
            Passed while the final output was bad — a verifier that rubber-stamps bad
            work is one of the most expensive multi-agent failure modes.
          </p>
          <div className="ref-list ref-list-suspect">
            {verificationGaps.map((g) => (
              <RunRef key={g.run_id} runId={g.run_id} labelFor={labelFor} onSelectRun={onSelectRun} />
            ))}
          </div>
        </Panel>
      )}

      {evidence && evidence.notes.length > 0 && (
        <Panel title="Reasoning">
          <ul className="notes">
            {evidence.notes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </Panel>
      )}

      {scoreEntries.length > 0 && (
        <Panel title="Score map">
          <div className="score-map">
            {scoreEntries.map(([runId, score]) => (
              <div key={runId} className="score-row">
                <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                <span className="score-chip" style={{ background: scoreColor(score) }}>
                  {formatScore(score)}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {evidence && evidence.candidacy && Object.keys(evidence.candidacy).length > 0 && (
        <Panel title="Why each node (candidacy)">
          <div className="judge-list">
            {Object.entries(evidence.candidacy).map(([runId, status]) => (
              <div key={runId} className="fact-found">
                <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                <span className="muted small">{status}</span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {dropEntries.length > 0 && (
        <Panel title="Quality drops">
          <div className="score-map">
            {dropEntries.map(([runId, drop]) => (
              <div key={runId} className="score-row">
                <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                <span className="drop-val">-{drop.toFixed(2)}</span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {judgeEntries.length > 0 && (
        <Panel title="Judge reasoning">
          <div className="judge-list">
            {judgeEntries.map(([runId, note]) => (
              <div key={runId} className="judge-item">
                <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                <p className="judge-note">{note}</p>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {evidence && evidence.fact_propagation && evidence.fact_propagation.length > 0 && (
        <Panel title="Fact propagation">
          <div className="fact-list">
            {evidence.fact_propagation.map((fact, i) => (
              <div key={i} className="fact-item">
                <p className="fact-claim">{fact.claim}</p>
                <div className="fact-found">
                  <span className="muted">found in:</span>
                  {fact.found_in.length === 0 ? (
                    <span className="muted"> none</span>
                  ) : (
                    fact.found_in.map((runId) => (
                      <RunRef
                        key={runId}
                        runId={runId}
                        labelFor={labelFor}
                        onSelectRun={onSelectRun}
                      />
                    ))
                  )}
                </div>
                {fact.not_checkable && fact.not_checkable.length > 0 && (
                  <div className="fact-found">
                    <span className="muted" title="No payload to inspect (e.g. the node failed) — not the same as 'not found'.">
                      not checkable:
                    </span>
                    {fact.not_checkable.map((runId) => (
                      <RunRef
                        key={runId}
                        runId={runId}
                        labelFor={labelFor}
                        onSelectRun={onSelectRun}
                      />
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Panel>
      )}

      {evidence && evidence.loop_anomalies.length > 0 && (
        <Panel title="Loop anomalies">
          <div className="loop-list">
            {evidence.loop_anomalies.map((loop, i) => (
              <div key={i} className="loop-item">
                <div>
                  <TypeBadge label={loop.limit_kind} /> {loop.iterations} iterations
                </div>
                <div className="muted">agents: {loop.agent_names.join(", ") || "-"}</div>
                {loop.baseline && (
                  <div className="muted">
                    baseline mean {loop.baseline.mean_iterations.toFixed(1)} / std{" "}
                    {loop.baseline.std_iterations.toFixed(1)} (n={loop.baseline.sample_count})
                  </div>
                )}
              </div>
            ))}
          </div>
        </Panel>
      )}

      {unscored.length > 0 && (
        <Panel title="Unscored nodes">
          <div className="ref-list">
            {unscored.map((runId) => (
              <RunRef key={runId} runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
            ))}
          </div>
          {evidence && evidence.unknown_ancestors.length > 0 && (
            <div className="muted small">
              unknown ancestors flagged: {evidence.unknown_ancestors.length}
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
