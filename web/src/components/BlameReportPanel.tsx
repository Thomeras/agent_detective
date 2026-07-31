// Renders the latest BlameReport for an incident (spec 6.4 side panel):
// report_type, culprit(s), confidence, and the evidence bundle -- score map,
// drops, judge reasoning, fact propagation, unscored nodes, downstream cost.

import type { ReportDetail } from "../api/types";
import {
  formatConfidence,
  formatCost,
  formatCoverage,
  formatScore,
  judgeLabel,
  shortId,
} from "../format";
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
    // Deliberately NOT "culprit" wording: the whole point of this verdict is
    // "surface fragility, do not assign blame for a run that ended fine".
    case "degraded_recovered":
      return plural
        ? "Fragile nodes — degraded but recovered"
        : "Fragile node — degraded but recovered";
    // Escalation of degraded_recovered: the CONTENT recovered, but a verified
    // contract breach shipped — a silent failure in production, not a near-miss.
    case "shipped_with_latent_defect":
      return "Origin — silent defect shipped";
    // Rubric split: the listed node is the CONTRACT fault's origin only; the
    // content defect observed at the terminal has no localized origin.
    case "terminal_defect_unlocalized":
      return "Contract origin — terminal content defect not localized";
    case "unclassified":
      return plural ? "Possible suspects" : "Possible suspect";
    default:
      return plural ? "Suspected culprits" : "Suspected culprit";
  }
}

const FALLBACK_NOTE: Record<string, string> = {
  composition_failure:
    "No single node broke — the fault could not be localised. The suspect is the orchestration/task-design LAYER, not any node's own work.",
  root_cause_external:
    "The fault originated outside the observed graph (the input was already flawed).",
  verification_gap:
    "These verifiers passed the work while the final output was bad — they let it through unflagged.",
  degraded_recovered:
    "This node underperformed, but every downstream step and the terminal deliverable recovered — a near-miss the pipeline compensated for, not a live quality break. Surfaced as a fragile point to harden, not an outage.",
  shipped_with_latent_defect:
    "The pipeline recovered the content, but a VERIFIED contract breach shipped in the deliverable. The terminal judge verifies content, not carried contract parameters, so it could not catch this — a silent failure reached production, not a near-miss.",
  terminal_defect_unlocalized:
    "The terminal deliverable is bad on CONTENT, but no node qualifies as a content origin — the only localized fault is a contract breach whose node's content the judge scored healthy. The attribution shown is the contract fault's, NOT blame for the terminal content defect.",
};

const GAP_BASIS_LABEL: Record<string, string> = {
  verdict_scored_incorrect: "verdict itself judged wrong",
  passed_bad_terminal: "passed work behind a bad terminal verdict",
  // Not an accusation: the judge's own flag and its own score contradict each
  // other, and the engine does not know which one failed.
  verifier_flag_conflict: "unresolved — FAIL flag contradicts the score",
};

// Reasoning notes are long evidence prose, each opening with a stable
// "slug:" prefix written by the engine/worker. The panel renders a short
// human headline per note (what roughly happened) with the full text behind
// a disclosure. The mapping is PRESENTATION ONLY — the complete note is
// always available expanded; nothing semantic keys off these strings.
const NOTE_TITLES: Record<string, string> = {
  no_scores: "No scores available",
  root_cause_external: "Fault entered from outside the graph",
  loop_detected: "Runaway loop detected",
  cut_point: "Origin localised — where quality broke",
  "cut_point (cumulative degradation)": "Origin localised — slow quality erosion",
  "cut_point (fabrication cascade)":
    "Origin localised — content went missing, downstream claimed success",
  multi_culprit: "Multiple independent origins",
  composition_failure: "No single node broke — orchestration suspected",
  unclassified: "No origin localised",
  degraded_recovered: "Degraded but recovered (near-miss)",
  terminal_not_checkable: "Terminal verdict discarded — judge never saw the deliverable",
  instrumentation_warning: "Instrumentation gap — nodes without payloads",
  verdict_conflict: "Terminal verdict contradicts a healthy score",
  verification_gap: "Verifier passed bad work",
  claims_vs_reality: "Healthy score contradicted by ground truth",
  cascade_participants: "Downstream success built on missing content",
  contract_vs_terminal: "Contract breach vs. ok terminal — the judge is blind to the contract",
  terminal_defect_unlocalized:
    "Terminal content defect observed — origin not localized",
  form_defect_shipped: "Wrong form shipped — no verifier owns form/contract vision",
  requirement_provenance:
    "Contract reference is scaffold — it does not match the user's quoted requirement",
  escalation: "Verdict escalated — silent failure shipped to production",
  evidence_tension: "Evidence streams disagree (tension)",
  representation_divergence: "Terminal saw a different artifact than the verifiers",
};

function noteHeadline(note: string): string {
  const idx = note.indexOf(":");
  if (idx > 0 && idx < 60) {
    const slug = note.slice(0, idx).trim();
    // contract_propagation carries its outcome in the body — surface it.
    if (slug === "contract_propagation") {
      if (note.includes("PROPAGATED")) return "Breach propagated into the shipped artifact (verified)";
      if (note.includes("corrected downstream")) return "Breach corrected downstream (verified)";
      return "Breach propagation unverified";
    }
    const mapped = NOTE_TITLES[slug];
    if (mapped) return mapped;
  }
  // Unknown prefix: fall back to a trimmed excerpt so nothing renders blank.
  return note.length > 80 ? `${note.slice(0, 80)}…` : note;
}

// ---- Verdict banner: passed/failed at a glance. PRESENTATION ONLY — the
// mapping is keyed off report_type; the full evidence is always below.
const TYPE_WHY: Record<string, string> = {
  cut_point: "Quality demonstrably broke at the origin.",
  multi_culprit: "Several independent origins broke quality.",
  shipped_with_latent_defect:
    "A verified contract breach shipped in the deliverable — the terminal judge passed it because it cannot see carried contract parameters.",
  degraded_recovered:
    "A node underperformed, but every downstream step recovered and the terminal deliverable is ok — a near-miss, not an outage.",
  loop_detected: "A runaway loop burned iterations past the limit.",
  verification_gap: "A verifier passed work it should have failed.",
  composition_failure:
    "No single node broke — the orchestration/task design is suspected.",
  root_cause_external: "The fault entered from outside the observed graph.",
  terminal_defect_unlocalized:
    "The terminal content is bad, but no node qualifies as a content origin — only a contract fault is localized. The content defect's source is unknown.",
  unclassified: "No failure was localised.",
};

function verdictOf(reportType: string | null): { label: string; kind: string } {
  switch (reportType) {
    case "degraded_recovered":
      return { label: "PASSED — with warnings", kind: "warning" };
    case "unclassified":
      return { label: "INCONCLUSIVE", kind: "inconclusive" };
    case null:
      return { label: "INCONCLUSIVE", kind: "inconclusive" };
    default:
      // Every localising verdict (cut_point, latent defect, loop, gap, …)
      // means the run did NOT pass.
      return { label: "FAILED", kind: "failed" };
  }
}

// First sentence of the body (after the slug), for the collapsed gist line.
function noteGist(note: string): string {
  const idx = note.indexOf(":");
  const body = idx > 0 && idx < 60 ? note.slice(idx + 1).trim() : note;
  const stop = body.indexOf(". ");
  const gist = stop > 0 ? body.slice(0, stop + 1) : body;
  return gist.length > 140 ? `${gist.slice(0, 140)}…` : gist;
}

// Sort per-node record entries into deterministic topological order (JSONB
// scrambles object key order; topo_order is the authoritative pipeline order).
function orderByTopo<T>(entries: [string, T][], topo: string[] | undefined): [string, T][] {
  if (!topo || topo.length === 0) return entries;
  const index = new Map(topo.map((id, i) => [id, i]));
  return [...entries].sort(
    ([a], [b]) => (index.get(a) ?? topo.length) - (index.get(b) ?? topo.length),
  );
}

export default function BlameReportPanel({ report, labelFor, onSelectRun }: BlameReportPanelProps) {
  const evidence = report.evidence;
  const culprits = report.culprit_run_ids ?? [];
  const unscored = report.unscored_run_ids ?? [];
  const fallbackNote = report.report_type ? FALLBACK_NOTE[report.report_type] : undefined;
  const manifestation = evidence?.manifestation_run_ids ?? [];
  const verificationGaps = evidence?.verification_gaps ?? [];
  const terminalVerdict = evidence?.terminal_verdict ?? null;
  const degradationPaths = evidence?.degradation_paths ?? [];
  // Competing origin hypotheses: present ONLY when the evidence streams disagree
  // on where the fault started and the engine could not resolve it. Its presence
  // means the single headline origin/confidence is not settled — so we surface
  // the breakdown and mark the confidence as split.
  const hypotheses = evidence?.hypotheses ?? [];
  // Confidence split: observation = how sure the output is defective;
  // attribution = how sure the fault originated here rather than inherited.
  // Either may be absent on older reports.
  const observationConfidence = evidence?.observation_confidence ?? null;
  const attributionConfidence = evidence?.attribution_confidence ?? null;
  // Per-defect attribution entries (contract vs content strength differ).
  const attributionBreakdown = evidence?.attribution_breakdown ?? [];
  // Deterministic input-contract breaches — a hard diff, separate provenance
  // from the LLM judge. Empty when none.
  const contractViolations = evidence?.contract_violations ?? [];
  // Named reproducible checks with provenance "deterministic" (never the LLM
  // judge) — see docs/deterministic-signals.md for the signal contract.
  const deterministicSignals = evidence?.deterministic_signals ?? [];
  // How many of the affected runs carried a price behind downstream_cost_usd.
  const costCoverage = formatCoverage(report.cost_coverage);
  const topoOrder = evidence?.topo_order;
  const verifierIds = new Set(evidence?.verifier_run_ids ?? []);
  const nodeFlags = evidence?.node_flags ?? {};
  const isCompositionFailure = report.report_type === "composition_failure";
  const candidacy = evidence?.candidacy ?? {};
  const overrides = new Map(
    (evidence?.score_overrides ?? []).map((o) => [o.run_id, o]),
  );
  // Cascade participants and claims-vs-reality producers keep a healthy NUMBER
  // in the score map, but the blame engine has already ruled that number an
  // *unverified claim* (both candidacy traces end in that exact phrase). Render
  // those scores with an "unverified" badge so "0.93" never reads as a clean
  // pass — the number stands, but not as independent evidence of quality. These
  // nodes carry no score_override (that vehicle is only for refuted verifier
  // verdicts), so the candidacy trace is the honest signal to key off.
  const unverifiedClaims = new Map(
    Object.entries(candidacy).filter(([, note]) => note.includes("unverified claim")),
  );
  // A node that is BOTH plain-unscored and an unknown ancestor was being listed
  // twice at the report end (once in the flat list, once in the confidence-cap
  // line). The cap line is the informative placement, so drop those ids from the
  // flat list — each unscored-upstream node appears exactly once.
  const unknownAncestors = evidence?.unknown_ancestors ?? [];
  const unknownAncestorSet = new Set(unknownAncestors);
  const plainUnscored = unscored.filter((r) => !unknownAncestorSet.has(r));
  // "Where it surfaced" only carries information when it DIFFERS from the
  // origin — origin == manifestation restated is noise.
  const surfacedElsewhere = manifestation.filter((r) => !culprits.includes(r));

  const scoreEntries = evidence ? orderByTopo(Object.entries(evidence.score_map), topoOrder) : [];
  const pipelineScores = scoreEntries.filter(([runId]) => !verifierIds.has(runId));
  const verifierScores = scoreEntries.filter(([runId]) => verifierIds.has(runId));
  const dropEntries = evidence ? orderByTopo(Object.entries(evidence.drops), topoOrder) : [];
  const judgeEntries = evidence ? orderByTopo(Object.entries(evidence.judge_notes), topoOrder) : [];
  const candidacyEntries =
    evidence && evidence.candidacy
      ? orderByTopo(Object.entries(evidence.candidacy), topoOrder)
      : [];

  // A judged score refuted by ground truth renders as: struck original ("claimed")
  // → effective value, each captioned so the two numbers are never a bare
  // "0.56 / 0.10". The reason is on hover. Never silently rewritten. A score the
  // engine ruled an unverified claim (cascade/reality) keeps its number but wears
  // an "unverified" badge so it is not mistaken for a clean pass.
  function renderScoreRow(runId: string, score: number | null, isVerifier: boolean) {
    const ov = overrides.get(runId);
    const unverified = unverifiedClaims.get(runId);
    return (
      <div key={runId} className={`score-row${isVerifier ? " score-row-verifier" : ""}`}>
        <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
        {ov ? (
          <span className="score-cell score-override" title={ov.reason}>
            <span className="chip-col">
              <span className="chip-caption">claimed</span>
              <span className="score-chip score-refuted">{formatScore(ov.original)}</span>
            </span>
            <span className="score-arrow" aria-hidden>
              →
            </span>
            <span className="chip-col">
              <span className="chip-caption">effective</span>
              <span className="score-chip" style={{ background: scoreColor(ov.effective) }}>
                {formatScore(ov.effective)}
              </span>
            </span>
          </span>
        ) : unverified ? (
          <span className="score-cell">
            <span className="score-chip" style={{ background: scoreColor(score) }}>
              {formatScore(score)}
            </span>
            <span className="unverified-badge" title={unverified}>
              unverified
            </span>
          </span>
        ) : (
          <span className="score-chip" style={{ background: scoreColor(score) }}>
            {formatScore(score)}
          </span>
        )}
      </div>
    );
  }

  // ---- Verdict banner: the answer FIRST (passed/failed, why, where), the
  // ---- detailed evidence below. verdictOf keys off report_type; the why-line
  // ---- is a static per-type sentence + the gist of the classification note.
  const verdict = verdictOf(report.report_type);
  const whyLine = report.report_type ? TYPE_WHY[report.report_type] : undefined;
  const firstNoteGist =
    evidence && evidence.notes.length > 0 ? noteGist(evidence.notes[0]) : null;

  return (
    <div className="blame-panel">
      <Panel title={<span className="blame-head">Verdict</span>}>
        <div className={`verdict-banner verdict-${verdict.kind}`}>
          <span className="verdict-status">{verdict.label}</span>
          <TypeBadge label={report.report_type ?? "unclassified"} />
          {terminalVerdict && (
            <span className="verdict-terminal muted small">
              terminal:{" "}
              {terminalVerdict.checkable === false
                ? "not verifiable"
                : terminalVerdict.bad
                  ? "bad"
                  : "ok"}
              {terminalVerdict.caveat ? " ⚠" : ""}
            </span>
          )}
        </div>
        {culprits.length > 0 && !isCompositionFailure && (
          <div className="verdict-where">
            <span className="kv-key">Where</span>
            <span className="ref-list">
              {culprits.map((runId) => (
                <RunRef key={runId} runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
              ))}
            </span>
          </div>
        )}
        {isCompositionFailure && (
          <div className="verdict-where">
            <span className="kv-key">Where</span>
            <span className="layer-suspect">Orchestration / task design</span>
          </div>
        )}
        {(whyLine || firstNoteGist) && (
          <div className="verdict-why">
            <span className="kv-key">Why</span>
            <span>
              {whyLine && <span className="verdict-why-line">{whyLine}</span>}
              {firstNoteGist && (
                <span className="muted small verdict-why-gist"> {firstNoteGist}</span>
              )}
            </span>
          </div>
        )}
      </Panel>

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
            <span className="kv-val">
              {/* NO single headline number when the split exists: the headline
                  semantics differ per verdict type (attribution for cut_point,
                  observation for shipped_with_latent_defect), so one big
                  figure reads as one scale across reports and "certainty rose"
                  when attribution was honestly lowered. Two equal, labelled
                  numbers cannot be misread that way. */}
              {report.report_type === "unclassified" || report.confidence == null
                ? "—"
                : observationConfidence == null && attributionConfidence == null
                  ? formatConfidence(report.confidence)
                  : null}
              {hypotheses.length > 0 && (
                <span
                  className="conf-split-tag"
                  title="The evidence streams disagree on the origin. This is the dominant hypothesis's share, not a settled figure — see competing origins below."
                >
                  split
                </span>
              )}
            </span>
            {/* A single headline confidence hides two independent questions:
                is the output actually defective, and did the fault start here?
                When the engine reports them, show the breakdown so a confident
                "output is bad" is never conflated with a confident "it started
                here" (or vice versa). */}
            {(observationConfidence != null || attributionConfidence != null) && (
              <>
                <span className="conf-breakdown">
                  {observationConfidence != null && (
                    <span className="chip-col">
                      <span className="chip-caption">observed defect</span>
                      <span className="score-chip conf-chip">
                        {formatConfidence(observationConfidence)}
                      </span>
                    </span>
                  )}
                  {attributionConfidence != null && (
                    <span className="chip-col">
                      <span className="chip-caption">attribution</span>
                      <span className="score-chip conf-chip">
                        {formatConfidence(attributionConfidence)}
                      </span>
                    </span>
                  )}
                </span>
                <span className="muted small">
                  observed defect = how sure the output is faulty; attribution =
                  how sure the fault originated here (not inherited). The
                  attribution shown is that of the defect carrying the verdict —
                  per-defect values below when they differ.
                </span>
                {/* Per-defect attribution: a contract breach OBSERVES both
                    sides (origination near-certain) while a content defect at
                    the observability boundary is capped — one blended number
                    would take the worse and undersell the stronger claim. */}
                {attributionBreakdown.length > 1 && (
                  <span className="attr-breakdown">
                    {attributionBreakdown.map((d, i) => (
                      <span key={i} className="muted small attr-breakdown-row" title={d.basis}>
                        {d.defect}: <strong>{formatConfidence(d.attribution)}</strong>
                      </span>
                    ))}
                  </span>
                )}
              </>
            )}
          </div>
          <div className="kv">
            <span className="kv-key">Downstream cost</span>
            <span className="kv-val">
              {/* Cost is "cost downstream of the culprit" — without a culprit
                  there is nothing to attribute, and "$0.00" reads as a claim. */}
              {culprits.length === 0 ? "—" : formatCost(report.downstream_cost_usd)}
            </span>
            {/* A total summed over the runs that carried a price is a lower
                bound; unpriced runs are unknown spend, not free. */}
            {culprits.length > 0 && (
              <span className="muted small">
                {costCoverage ?? "cost coverage not recorded"}
              </span>
            )}
          </div>
        </div>

        <div className="blame-section">
          <div className="blame-label">
            {culpritHeading(report.report_type, culprits.length > 1)}
          </div>
          {isCompositionFailure ? (
            // A fallback verdict suspects a LAYER, not a node: showing a node
            // chip here while candidacy calls the same node "not a culprit"
            // reads as a contradiction. Name the layer; the entry node is
            // secondary context.
            <div>
              <span className="layer-suspect">Orchestration / task design</span>
              {culprits.length > 0 && (
                <div className="muted small">
                  enters the graph at{" "}
                  {culprits.map((runId) => (
                    <RunRef key={runId} runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                  ))}
                </div>
              )}
            </div>
          ) : culprits.length === 0 ? (
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

        {hypotheses.length > 0 && (
          <div className="blame-section">
            <div
              className="blame-label"
              title="The independent evidence streams disagree on where the fault started. The reported origin stays dominant, but a later render/export step could not be ruled out — so the confidence is split across these hypotheses rather than claimed for one."
            >
              Competing origins (unresolved)
            </div>
            <div className="hypothesis-list">
              {hypotheses.map((h, i) => {
                const pct = Math.round(h.weight * 100);
                return (
                  <div key={i} className="hypothesis-row">
                    <span className="hypothesis-origin">
                      {h.origin ? (
                        <RunRef runId={h.origin} labelFor={labelFor} onSelectRun={onSelectRun} />
                      ) : (
                        <span className="muted">unresolved</span>
                      )}
                    </span>
                    <span className="hypothesis-bar-track" aria-hidden>
                      <span
                        className={`hypothesis-bar${h.origin ? "" : " hypothesis-bar-unresolved"}`}
                        style={{ width: `${pct}%` }}
                      />
                    </span>
                    <span className="hypothesis-weight">{pct}%</span>
                    <span className="hypothesis-basis muted small">{h.basis}</span>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {surfacedElsewhere.length > 0 && (
          <div className="blame-section">
            <div
              className="blame-label"
              title="The terminal artifact/output the failure surfaced in — shown only when it differs from the origin. Verifier sinks (qa/eval) issue verdicts, they do not manifest anything — they are mapped back to the producer."
            >
              Failure surfaced in output of
            </div>
            <div className="ref-list">
              {surfacedElsewhere.map((runId) => (
                <RunRef key={runId} runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
              ))}
            </div>
          </div>
        )}

        {terminalVerdict && (
          <div className="blame-section">
            <div
              className="blame-label"
              title="The tier1 terminal-judge verdict this classification leaned on — the evidence behind any 'terminal is bad' claim."
            >
              Terminal verdict
              {/* Which tier decided: a deterministic deliverable check (tier0)
                  skips the LLM judge entirely — labelling that verdict
                  "tier1 judge" would misattribute the decision. */}
              <span className="muted small terminal-decider">
                {terminalVerdict.decided_by === "deterministic"
                  ? " — decided by tier0 deterministic check"
                  : " — decided by tier1 LLM judge"}
              </span>
            </div>
            {/* Two INDEPENDENT axes: CONTENT (what the verdict judged) and
                CONTRACT (format/params conformance). One sentence mixing them
                self-contradicts the moment both fail at once. */}
            <div className="terminal-axes">
              <div className="terminal-axis">
                <span className="kv-key">Content</span>
                {terminalVerdict.stale ? (
                  // A stale verdict is NOT a live "bad": its deterministic
                  // basis no longer reproduces — and the rule-set fingerprint
                  // settles WHY (rule change vs artifact divergence).
                  <span title={terminalVerdict.stale_cause ?? undefined}>
                    <TypeBadge label="stale — not reproducible" />
                  </span>
                ) : terminalVerdict.checkable === false ? (
                  <TypeBadge label="not verifiable" />
                ) : (
                  <TypeBadge label={terminalVerdict.bad ? "bad" : "ok"} />
                )}
                {terminalVerdict.checkable !== false && terminalVerdict.score != null && (
                  <span className="muted small"> score {formatScore(terminalVerdict.score)}</span>
                )}
              </div>
              {terminalVerdict.contract_conformance && (
                <div className="terminal-axis">
                  <span className="kv-key">Contract</span>
                  <TypeBadge
                    label={
                      terminalVerdict.contract_conformance.startsWith("nonconformant")
                        ? "nonconformant"
                        : terminalVerdict.contract_conformance.startsWith("restored")
                          ? "restored"
                          : "unverified"
                    }
                    kind={
                      terminalVerdict.contract_conformance.startsWith("nonconformant")
                        ? "fail"
                        : undefined
                    }
                  />
                  <span className="muted small"> {terminalVerdict.contract_conformance}</span>
                </div>
              )}
            </div>
            {/* A caveat qualifies the verdict — typically an "ok" that holds
                for CONTENT only while a carried contract parameter shipped
                breached/unverified. It must sit right under the badge, or the
                header ("ok 1.00") lies about a nonconformant deliverable. */}
            {terminalVerdict.stale_cause && (
              <p className="suspect-note">{terminalVerdict.stale_cause}</p>
            )}
            {terminalVerdict.caveat && (
              <p className="terminal-caveat">{terminalVerdict.caveat}</p>
            )}
            {/* The "judge never saw the deliverable" narrative is for the
                OPAQUE case only. A stale verdict has its own story (the
                stale_cause above) — its checker DID see the deliverable, the
                basis just no longer reproduces. */}
            {terminalVerdict.checkable === false && !terminalVerdict.stale && (
              <p className="suspect-note">
                No ground truth — the terminal judge never saw the final
                deliverable's content, so no "bad" conclusion is drawn from it.
              </p>
            )}
            {terminalVerdict.reasoning && (
              <p className="judge-note">{terminalVerdict.reasoning}</p>
            )}
          </div>
        )}
      </Panel>

      {degradationPaths.length > 0 && (
        <Panel title="Degradation path">
          <p className="suspect-note">
            No single step crossed the drop threshold, but quality eroded past the
            cumulative threshold across consecutive nodes.
          </p>
          {degradationPaths.map((chain, i) => (
            <div key={i} className="degradation-chain">
              <div className="ref-list">
                {chain.path.map((runId, j) => (
                  <span key={runId} className="degradation-step">
                    {j > 0 && <span className="muted"> → </span>}
                    <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                    <span className="score-chip" style={{ background: scoreColor(chain.scores[j]) }}>
                      {formatScore(chain.scores[j])}
                    </span>
                  </span>
                ))}
              </div>
              <div className="drop-val">cumulative -{chain.cumulative_drop.toFixed(2)}</div>
            </div>
          ))}
        </Panel>
      )}

      {verificationGaps.length > 0 && (
        <Panel title="Verification gaps">
          <p className="suspect-note">
            Verifiers whose PASS was wrong — a verifier that rubber-stamps bad
            work is one of the most expensive multi-agent failure modes.
          </p>
          <div className="judge-list">
            {verificationGaps.map((g) => (
              <div key={g.run_id} className="fact-found">
                <RunRef runId={g.run_id} labelFor={labelFor} onSelectRun={onSelectRun} />
                <span className="muted small">
                  {(g.basis && GAP_BASIS_LABEL[g.basis]) ?? g.basis ?? ""}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {evidence && evidence.notes.length > 0 && (
        <Panel title="Reasoning">
          {/* Each note: a headline of what roughly happened, expandable to the
              full evidence prose. Native <details> — no state to manage. */}
          <div className="note-list">
            {evidence.notes.map((note, i) => {
              // A finding refuted by later verified evidence gets the SAME
              // visual language as a refuted judge assessment — a tool that
              // marks its judges' superseded claims but not its own holds a
              // double standard. The note stays readable (ledger, not
              // erasure); the marker says it no longer stands.
              const superseded = (evidence.superseded_notes ?? []).find(
                (s) => note.startsWith(`${s.slug}:`),
              );
              return (
                <details key={i} className="note-item">
                  <summary className="note-summary">
                    <span className={superseded ? "note-title note-superseded" : "note-title"}>
                      {noteHeadline(note)}
                    </span>
                    <span className="note-gist muted small">{noteGist(note)}</span>
                  </summary>
                  {superseded && (
                    <p className="judge-refuted">
                      ⚠ this finding was superseded — {superseded.reason}
                    </p>
                  )}
                  <p className="note-full">{note}</p>
                </details>
              );
            })}
          </div>
        </Panel>
      )}

      {contractViolations.length > 0 && (
        <Panel title="Deterministic contract check">
          <p className="muted small">
            A hard input/output diff (not the LLM judge): a carried-through
            parameter this node silently rewrote.
          </p>
          <div className="judge-list">
            {contractViolations.map((v, i) => (
              <div key={`${v.run_id}-${v.key}-${i}`} className="fact-found">
                <RunRef runId={v.run_id} labelFor={labelFor} onSelectRun={onSelectRun} />
                <span className="contract-diff">
                  <span className="contract-key">{v.key}:</span>
                  <span className="score-chip contract-chip">{String(v.from)}</span>
                  <span className="score-arrow" aria-hidden>
                    →
                  </span>
                  <span className="score-chip contract-chip">{String(v.to)}</span>
                </span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {deterministicSignals.length > 0 && (
        <Panel title="Deterministic signals">
          <p className="muted small">
            Named reproducible checks (provenance: deterministic, not LLM) —
            see docs/deterministic-signals.md.
          </p>
          <div className="judge-list">
            {deterministicSignals.map((s, i) => (
              <div key={`${s.run_id}-${s.name}-${i}`} className="signal-item">
                <div className="fact-found">
                  <RunRef runId={s.run_id} labelFor={labelFor} onSelectRun={onSelectRun} />
                  <TypeBadge label={s.severity} kind={s.severity} />
                  <span className="signal-name">{s.name}</span>
                  <span>{s.detail}</span>
                </div>
                <div className="muted small" title={s.basis}>
                  basis: {s.basis}
                </div>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {/* Everything below is the audit trail — the verdict above already
          answers passed/failed, why and where. */}
      <div className="detail-divider">Detailed evidence</div>

      {scoreEntries.length > 0 && (
        <Panel title="Score map (topological order)">
          <div className="score-map">
            {pipelineScores.map(([runId, score]) => renderScoreRow(runId, score, false))}
            {verifierScores.length > 0 && (
              <div className="muted small score-group-label">verifiers</div>
            )}
            {verifierScores.map(([runId, score]) => renderScoreRow(runId, score, true))}
          </div>
          {(unverifiedClaims.size > 0 || overrides.size > 0) && (
            <p className="muted small">
              {overrides.size > 0 && (
                <>claimed → effective: a judged score ground truth refuted. </>
              )}
              {unverifiedClaims.size > 0 && (
                <>
                  unverified: the number stands as the node's own claim, not as
                  independent evidence of quality.
                </>
              )}
            </p>
          )}
        </Panel>
      )}

      {candidacyEntries.length > 0 && (
        <Panel title="Why each node (candidacy)">
          <div className="judge-list">
            {candidacyEntries.map(([runId, status]) => (
              <div key={runId} className="fact-found">
                <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                <span className="muted small">
                  {status}
                  {nodeFlags[runId] && nodeFlags[runId].length > 0 && (
                    <> — flags: {nodeFlags[runId].join(", ")}</>
                  )}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {dropEntries.length > 0 && (
        <Panel title="Quality drops">
          <p className="muted small">
            Each drop is measured from a node's best-scored predecessor (from) to
            its own score (to).
          </p>
          <div className="score-map">
            {dropEntries.map(([runId, drop]) => {
              // The engine defines drop = best_predecessor_score − own_score, so
              // the "from" value is recoverable as own_score + drop. Showing both
              // ends turns a bare "-0.36" into an auditable "0.92 → 0.56".
              const to = evidence?.score_map[runId] ?? null;
              const from = to === null ? null : to + drop;
              return (
                <div key={runId} className="score-row">
                  <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                  <span className="drop-cell">
                    {from !== null && to !== null && (
                      <>
                        <span
                          className="score-chip"
                          title="best-scored predecessor"
                          style={{ background: scoreColor(from) }}
                        >
                          {formatScore(from)}
                        </span>
                        <span className="score-arrow" aria-hidden>
                          →
                        </span>
                        <span
                          className="score-chip"
                          title="this node's score"
                          style={{ background: scoreColor(to) }}
                        >
                          {formatScore(to)}
                        </span>
                      </>
                    )}
                    <span className="drop-val">-{drop.toFixed(2)}</span>
                  </span>
                </div>
              );
            })}
          </div>
        </Panel>
      )}

      {judgeEntries.length > 0 && (
        <Panel title="Judge reasoning">
          {/* The prose and the judged numbers name their instrument, or say
              plainly that it was never recorded. */}
          <p className="muted small">judged by {judgeLabel(report.judge_model)}</p>
          <div className="judge-list">
            {judgeEntries.map(([runId, note]) => (
              <div key={runId} className="judge-item">
                <RunRef runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
                {/* A refuted node's fluent praise must not stand unannotated —
                    readers trust prose over numbers. */}
                {overrides.has(runId) && (
                  <p className="judge-refuted" title={overrides.get(runId)?.reason}>
                    ⚠ this assessment was refuted — {overrides.get(runId)?.reason}
                  </p>
                )}
                <p className="judge-note">{note}</p>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {/* Fact propagation is null when the check never ran (it only runs when a
          fabrication cascade is suspected AND the terminal deliverable was
          visible), [] when it ran but found no checkable claims, and populated
          otherwise. A silently missing section conflates "did not run" with
          "found nothing" — render the state explicitly so the user knows which. */}
      {evidence && evidence.fact_propagation === null && (
        <Panel title="Fact propagation">
          <p className="muted small">
            Propagation not evaluated — this trace runs only when a fabrication
            cascade is suspected and the terminal deliverable's content was
            visible. It did not run for this report.
          </p>
        </Panel>
      )}

      {evidence && evidence.fact_propagation !== null && evidence.fact_propagation.length === 0 && (
        <Panel title="Fact propagation">
          <p className="muted small">
            Evaluated — no checkable factual claims were found to trace across the
            pipeline.
          </p>
        </Panel>
      )}

      {evidence && evidence.fact_propagation && evidence.fact_propagation.length > 0 && (
        <Panel title="Fact propagation">
          <div className="fact-list">
            {evidence.fact_propagation.map((fact, i) => (
              <div key={i} className="fact-item">
                <p className="fact-claim">
                  {fact.claim}
                  {fact.source && (
                    <span className="muted small fact-source"> [{fact.source}]</span>
                  )}
                </p>
                <div className="fact-found">
                  <span className="muted">found in:</span>
                  {fact.found_in.length === 0 ? (
                    // A REQUIRED element found nowhere is the headline
                    // evidence — render it loud, never as muted "none".
                    fact.source === "required" && (fact.checked ?? 0) > 0 ? (
                      <span className="fact-missing"> MISSING everywhere ({fact.checked} payloads checked)</span>
                    ) : (
                      <span className="muted"> none</span>
                    )
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
          {/* Unknown ancestors are unscored nodes too, but they get their own
              annotated line below — listing them here as well printed each id
              twice. Show only the ids the cap line does NOT cover. */}
          {plainUnscored.length > 0 && (
            <div className="ref-list">
              {plainUnscored.map((runId) => (
                <RunRef key={runId} runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
              ))}
            </div>
          )}
          {unknownAncestors.length > 0 && (
            <div className="muted small">
              <span title="Unscored nodes upstream of the culprit: the fault could in principle have started in one of them, which caps the report's confidence.">
                unscored upstream of the culprit (caps confidence):
              </span>{" "}
              {unknownAncestors.map((runId) => (
                <RunRef key={runId} runId={runId} labelFor={labelFor} onSelectRun={onSelectRun} />
              ))}
              <p className="suspect-note">
                Confidence is capped because any of these hidden nodes could be
                the real origin — with no score for them, the report cannot rule
                them out.
              </p>
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
