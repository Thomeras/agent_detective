// The ONE module owning presentation semantics (verdict-refactor-plan.md §9.3).
//
// This is the future replacement for the scattered maps in BlameReportPanel.tsx
// (culpritHeading + FALLBACK_NOTE + NOTE_TITLES + TYPE_WHY + verdictOf). Every
// label/tone/why-sentence is derived HERE, from typed kind+origin enums — never
// keyed off note-string prefixes (§1: "The UI keys off note-string prefixes …
// Rewording is a breaking change").
//
// Pure functions only, no React, no I/O — trivially unit-testable.

import type { DefectKind, Origin, OriginKind, ReportType } from "./types";

// Visual tone shared with the design tokens / primitives (§9.3). `unknown` is a
// first-class tone: an inconclusive/unlocalized state is not a failure and must
// not be coloured as one.
export type Tone = "ok" | "warn" | "fail" | "unknown";

// The uniform shape every descriptor returns (§9.3: `{label, tone, template}`).
// `template` is a human sentence describing the thing; it interpolates ONLY the
// caller-supplied structured fields (§2.4 no-unsupported-sentence) — here it is
// a static, kind-derived string with a `{origin}` slot the caller fills.
export interface Descriptor {
  label: string;
  tone: Tone;
  template: string;
}

// ---------------------------------------------------------------------------
// Origin phrasing (§2.2 sum type) — one place turns an Origin into prose.
// ---------------------------------------------------------------------------

// A one-letter qualifier for a non-localized origin, used in dense chip labels
// (`content?U`, `content?X`, `content?D`). Localized origins have no qualifier
// (the node is named instead). Keyed off the typed Origin kind — never a string
// prefix (§1 / constraint: no string-prefix keying in new code).
export function originQualifier(kind: OriginKind): "" | "U" | "X" | "D" {
  switch (kind) {
    case "localized":
      return "";
    case "unlocalized":
      return "U";
    case "external":
      return "X";
    case "design":
      return "D";
    default: {
      const _never: never = kind;
      return _never;
    }
  }
}

// The tone a lone origin variant contributes, exported for chip rendering.
export function toneForOrigin(kind: OriginKind): Tone {
  return originTone(kind);
}

// A short noun phrase for where the defect sits, given its origin variant.
// `runLabel` maps a run_id to a human node label (the caller owns identity).
export function originPhrase(origin: Origin, runLabel?: (runId: string) => string): string {
  switch (origin.kind) {
    case "localized":
      return runLabel ? runLabel(origin.run_id) : origin.run_id;
    case "unlocalized":
      // `reason` is a CODE; `reason_label` is its one phrasing (normalizeOrigin
      // resolves it, and passes pre-collapse prose through unchanged).
      return `origin not localized (${origin.reason_label ?? origin.reason})`;
    case "external":
      return "an external / upstream input";
    case "design": {
      const why = origin.reason_label ?? origin.reason;
      return why
        ? `the graph's design (${why})`
        : "the graph's design — no node owns the check";
    }
    default: {
      // Exhaustiveness guard: a new Origin variant forces a compile error here.
      const _never: never = origin;
      return _never;
    }
  }
}

// The tone a lone origin variant contributes (a localized fault is a real
// failure; an unlocalized/design gap is a warning, not a proven break).
function originTone(kind: OriginKind): Tone {
  switch (kind) {
    case "localized":
      return "fail";
    case "external":
      return "warn";
    case "unlocalized":
      return "unknown";
    case "design":
      return "warn";
    default: {
      const _never: never = kind;
      return _never;
    }
  }
}

// ---------------------------------------------------------------------------
// defectDescriptor(kind, origin) — the per-defect card heading + why (§9.3)
// ---------------------------------------------------------------------------

// Human noun for each defect kind, plus the base "why" it matters. The origin
// determines the final tone and completes the template's `{origin}` slot.
const DEFECT_KIND_META: Record<DefectKind, { noun: string; why: string }> = {
  contract: {
    noun: "Contract breach",
    why: "A carried input/output parameter was silently rewritten",
  },
  content: {
    noun: "Content defect",
    why: "The deliverable's substance is wrong or missing",
  },
  form: {
    noun: "Form defect",
    why: "The wrong artifact form was shipped (extension/kind mismatch)",
  },
  loop: {
    noun: "Runaway loop",
    why: "Iterations ran past the expected limit",
  },
  verification: {
    noun: "Verification gap",
    why: "A verifier passed work it should have failed",
  },
};

export function defectDescriptor(kind: DefectKind, origin: Origin): Descriptor {
  const meta = DEFECT_KIND_META[kind];
  // A localized content/contract/form/loop fault is a failure; the same defect
  // left unlocalized is only a warning (we observed it but cannot attribute it).
  const tone = originTone(origin.kind);
  return {
    label: meta.noun,
    tone,
    // `{origin}` is filled by the caller via originPhrase() so node identity
    // stays out of this pure module. The sentence claims nothing beyond the
    // defect's own kind + origin (§2.4).
    template: `${meta.why} at {origin}.`,
  };
}

// ---------------------------------------------------------------------------
// verdictDescriptor(reportType) — the run-level answer (§9.2 header)
// ---------------------------------------------------------------------------
//
// Replaces verdictOf (label/kind) + TYPE_WHY (why sentence) in one table.
// `null` report_type is a genuinely unanalysed/inconclusive run, not a failure.

const VERDICT_META: Record<ReportType, Descriptor> = {
  cut_point: {
    label: "FAILED",
    tone: "fail",
    template: "Quality demonstrably broke at a localized origin.",
  },
  multi_culprit: {
    label: "FAILED",
    tone: "fail",
    template: "Several independent origins broke quality.",
  },
  composition_failure: {
    label: "FAILED",
    tone: "fail",
    template: "No single node broke — the orchestration / task design is suspected.",
  },
  loop_detected: {
    label: "FAILED",
    tone: "fail",
    template: "A runaway loop burned iterations past the limit.",
  },
  root_cause_external: {
    label: "FAILED",
    tone: "warn",
    template: "The fault entered from outside the observed graph.",
  },
  verification_gap: {
    label: "FAILED",
    tone: "fail",
    template: "A verifier passed work it should have failed.",
  },
  shipped_with_latent_defect: {
    label: "LATENT DEFECT",
    tone: "fail",
    template:
      "A verified contract breach shipped in the deliverable. The terminal content judge passed it (content rubric cannot see carried contract parameters); the detective's own form/propagation checks caught it post-hoc — no pipeline verifier owns contract/form vision.",
  },
  terminal_defect_unlocalized: {
    label: "FAILED",
    tone: "warn",
    template:
      "The terminal content is bad, but no node qualifies as a content origin — only a contract fault is localized. The content defect's source is unknown.",
  },
  degraded_recovered: {
    label: "PASSED — with warnings",
    tone: "warn",
    template:
      "A node underperformed, but every downstream step recovered and the terminal deliverable is ok — a near-miss, not an outage.",
  },
  unclassified: {
    label: "INCONCLUSIVE",
    tone: "unknown",
    template: "No failure was localised.",
  },
};

// A run with no report at all (never analysed / no evidence). Track C #1 makes
// this a first-class visible state instead of a silent gap (§9.2).
const UNANALYSED: Descriptor = {
  label: "UNANALYSED",
  tone: "unknown",
  template: "This run has not been analysed yet.",
};

export function verdictDescriptor(reportType: ReportType | null | undefined): Descriptor {
  if (reportType == null) return UNANALYSED;
  return VERDICT_META[reportType] ?? UNANALYSED;
}

// A clean run: analysed, no defect localised, no incident raised. Distinct from
// `unclassified` (INCONCLUSIVE — analysis ran but could not localise) and from
// UNANALYSED (§9.2 first-class state — analysis never ran). Kept here so the
// runs list speaks the same verdict vocabulary as the detail header.
export const PASSED_VERDICT: Descriptor = {
  label: "PASSED",
  tone: "ok",
  template: "The run completed cleanly — no defect was detected.",
};

// The pending/never-analysed verdict (Track C #1 made visible). Re-exported so
// callers do not reach for verdictDescriptor(null) by accident.
export const UNANALYSED_VERDICT: Descriptor = UNANALYSED;

// The culprit-section heading, keyed off report_type (replaces culpritHeading).
// Kept distinct from the verdict label because it names the OBJECT being blamed,
// not the run's pass/fail state.
const CULPRIT_HEADING: Record<ReportType, string> = {
  composition_failure: "Most likely: orchestration / design issue",
  root_cause_external: "Upstream / external cause",
  verification_gap: "Rubber-stamping verifier",
  cut_point: "Origin — where quality broke",
  degraded_recovered: "Fragile node — degraded but recovered",
  shipped_with_latent_defect: "Origin — silent defect shipped",
  terminal_defect_unlocalized: "Contract origin — terminal content defect not localized",
  multi_culprit: "Independent origins",
  loop_detected: "Loop origin",
  unclassified: "Possible suspect",
};

export function culpritHeading(reportType: ReportType | null | undefined, plural = false): string {
  const base = reportType ? CULPRIT_HEADING[reportType] : "Suspected culprit";
  if (!plural) return base ?? "Suspected culprit";
  // Naive pluralisation of the trailing noun for the few headings that take it.
  return (base ?? "Suspected culprit")
    .replace(/\bverifier\b/, "verifiers")
    .replace(/\bnode\b/, "nodes")
    .replace(/\bsuspect\b/, "suspects")
    .replace(/\borigin\b/, "origins");
}
