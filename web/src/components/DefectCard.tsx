// DefectCard — the primary object of the schema-2 run detail (§9.2).
//
// One card per Defect: kind + origin, the observation/attribution meter PAIR,
// propagation, caveat chips that NEVER truncate, and the Findings that evidence
// it behind a Disclosure INSIDE the card (evidence belongs to its defect, not a
// global notes list). Selecting a card highlights its propagation path on the
// canvas (the container owns that state and passes selected/onSelect).
//
// All labels/tones come from the verdict descriptor module — nothing here keys
// off a note-string prefix.

import type { Defect, DefectCaveat, Finding } from "../verdict/types";
import { defectDescriptor, originPhrase } from "../verdict/descriptor";
import { SPOKEN_KEYS, summarizeFinding } from "../verdict/findingSummary";
import { Badge, Card, Chip, Disclosure, Meter } from "../ui/primitives";

// Human labels for the structured caveat fields (§2.4). Typed kinds first, with
// a humanising fallback so an engine-added marker (e.g. "recovered") still
// renders as a chip instead of vanishing — a caveat is never dropped.
const CAVEAT_LABEL: Record<string, string> = {
  base_assumed: "baseline assumed",
  observability_boundary: "observability boundary",
  unverified_in_channel: "unverified in channel",
  recovered: "recovered downstream",
};

function caveatLabel(kind: string): string {
  return CAVEAT_LABEL[kind] ?? kind.replace(/_/g, " ");
}

function CaveatChips({ caveats }: { caveats?: DefectCaveat[] }) {
  if (!caveats || caveats.length === 0) return null;
  return (
    <div className="defect-caveats">
      {caveats.map((c, i) => (
        <Chip key={`${c.kind}-${i}`} tone="warn" title={c.detail ?? undefined}>
          {caveatLabel(c.kind)}
        </Chip>
      ))}
    </div>
  );
}

// One evidencing Finding, resolved from the report's findings[] by index.
//
// States the fact as a sentence and quotes the reasoning behind it. The raw
// payload stays one click away rather than being the primary view: a dump of
// every key in `data` is what made a real report read as a memory listing.
function FindingRow({
  finding,
  labelFor,
}: {
  finding: Finding;
  labelFor?: (runId: string) => string;
}) {
  const summary = summarizeFinding(finding, labelFor);
  const subject =
    finding.subject.scope === "run" && finding.subject.run_id
      ? (labelFor?.(finding.subject.run_id) ?? `run ${finding.subject.run_id.slice(0, 8)}`)
      : finding.subject.scope;
  const rest = Object.entries(finding.data ?? {}).filter(
    ([k]) => !SPOKEN_KEYS.has(k),
  );
  return (
    <div className="finding-row">
      <div className="finding-head">
        {/* No epistemic titles: "deterministic" = the rule fired reproducibly;
            "ground truth" is banned (it overclaimed, and it collides with the
            human-feedback label of the same name). */}
        <Badge channel={finding.channel} title={`${finding.channel} basis`}>
          {finding.channel === "deterministic" ? "deterministic" : "assessment"}
        </Badge>
        <span className="finding-subject dim">{subject}</span>
        <span className="finding-certainty mono dim" title="certainty of this finding">
          {Math.round(finding.certainty * 100)}%
        </span>
      </div>

      <p className="finding-headline">{summary.headline}</p>

      {summary.tags.length > 0 && (
        <div className="finding-tags">
          {summary.tags.map((tag) => (
            <span key={tag} className="finding-tag mono">
              {tag}
            </span>
          ))}
        </div>
      )}

      {summary.reasoning && (
        <blockquote className="finding-reasoning">{summary.reasoning}</blockquote>
      )}

      {finding.provenance?.quote && (
        <blockquote className="finding-quote">“{finding.provenance.quote}”</blockquote>
      )}

      <div className="finding-basis dim small">
        {finding.provenance?.label ? (
          <>
            basis: <span className="mono">{finding.provenance.label}</span>
            {finding.provenance.source ? ` · ${finding.provenance.source}` : ""}
          </>
        ) : (
          <>
            kind: <span className="mono">{finding.kind}</span>
          </>
        )}
      </div>

      {rest.length > 0 && (
        <details className="finding-raw">
          <summary className="small dim">raw payload ({rest.length})</summary>
          <dl className="finding-data">
            {rest.map(([k, v]) => (
              <div key={k} className="finding-data-row">
                <dt className="mono">{k}</dt>
                <dd className="mono">{renderValue(v)}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </div>
  );
}

function renderValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
    return String(v);
  }
  return JSON.stringify(v);
}

export default function DefectCard({
  defect,
  findings,
  labelFor,
  selected,
  onSelect,
}: {
  defect: Defect;
  findings: Finding[];
  labelFor: (runId: string) => string;
  selected: boolean;
  onSelect: () => void;
}) {
  const desc = defectDescriptor(defect.kind, defect.origin);
  const where = originPhrase(defect.origin, labelFor);
  // The card's sentence claims nothing beyond the defect's own kind + origin
  // (§2.4 no-unsupported-sentence): fill the descriptor's {origin} slot.
  const sentence = desc.template.replace("{origin}", where);

  // Refs carry polarity — never render counter-evidence under an "evidence
  // for this defect" heading (the run-15 bug: a content defect whose only
  // refs were a 1.0 score and an ok terminal).
  const resolved = defect.finding_refs
    .map((r) => ({ role: r.role, finding: findings[r.ref] }))
    .filter((x): x is { role: typeof x.role; finding: Finding } => Boolean(x.finding));
  const supporting = resolved.filter((x) => x.role === "supporting");
  const refuting = resolved.filter((x) => x.role === "refuting");
  const context = resolved.filter((x) => x.role === "context");

  const propagation = defect.propagation ?? [];

  return (
    <div
      className={`defect-card-wrap${selected ? " selected" : ""}`}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <Card
        tone={desc.tone}
        title={
          <span className="defect-title">
            {desc.label}
            <Badge channel={defect.channel} title={`${defect.channel} channel`}>
              {defect.channel === "deterministic" ? "deterministic" : "judged"}
            </Badge>
          </span>
        }
        actions={<Badge tone={desc.tone}>{originBadgeText(defect)}</Badge>}
      >
        <p className="defect-sentence">{sentence}</p>

        <Meter
          observation={defect.observation_confidence}
          attribution={defect.attribution_confidence}
          tone={desc.tone}
        />

        <CaveatChips caveats={defect.caveats} />

        {propagation.length > 0 && (
          <div className="defect-propagation">
            <span className="defect-propagation-label dim small">flowed through</span>
            <span className="defect-propagation-path">
              {propagation.map((runId, i) => (
                <span key={runId}>
                  {i > 0 && <span className="path-arrow" aria-hidden> → </span>}
                  <span className="mono">{labelFor(runId)}</span>
                </span>
              ))}
            </span>
          </div>
        )}

        {resolved.length > 0 && (
          // Stop card-selection when toggling the disclosure.
          <div onClick={(e) => e.stopPropagation()}>
            <Disclosure
              summary={`Findings (${resolved.length}) — ${supporting.length} supporting${
                refuting.length > 0 ? `, ${refuting.length} counter` : ""
              }`}
            >
              {supporting.length > 0 && (
                <div className="finding-group">
                  <div className="finding-group-label dim small">evidence for this defect</div>
                  <div className="finding-list">
                    {supporting.map((x, i) => (
                      <FindingRow
                        key={`s-${x.finding.kind}-${i}`}
                        finding={x.finding}
                        labelFor={labelFor}
                      />
                    ))}
                  </div>
                </div>
              )}
              {refuting.length > 0 && (
                <div className="finding-group">
                  <div className="finding-group-label dim small">
                    counter-evidence (kept visible — the defect stands on the channel above)
                  </div>
                  <div className="finding-list">
                    {refuting.map((x, i) => (
                      <FindingRow
                        key={`r-${x.finding.kind}-${i}`}
                        finding={x.finding}
                        labelFor={labelFor}
                      />
                    ))}
                  </div>
                </div>
              )}
              {context.length > 0 && (
                <div className="finding-group">
                  <div className="finding-group-label dim small">context</div>
                  <div className="finding-list">
                    {context.map((x, i) => (
                      <FindingRow
                        key={`c-${x.finding.kind}-${i}`}
                        finding={x.finding}
                        labelFor={labelFor}
                      />
                    ))}
                  </div>
                </div>
              )}
            </Disclosure>
          </div>
        )}
      </Card>
    </div>
  );
}

// The origin badge text: the node label for a localized defect, else the origin
// kind (Unlocalized / External / Design).
function originBadgeText(defect: Defect): string {
  switch (defect.origin.kind) {
    case "localized":
      return "Localized";
    case "unlocalized":
      return "Unlocalized";
    case "external":
      return "External";
    case "design":
      return "Design gap";
    default: {
      const _never: never = defect.origin;
      return _never;
    }
  }
}
