// One readable sentence per Finding, plus the reasoning that justifies it.
//
// Findings were rendered as a raw dump of `data`: every key a monospace <dt>,
// every value a <dd>, the same shape for a score, a flag, a contract breach and
// a terminal verdict. On a real report that reads as
//
//     assessment / content_flag / run 254fd14d / 70% / basis: per-node quality
//     judge / flag / missing_required_content / agent / plan
//
// which is a memory listing, not evidence. A reader cannot tell from it what
// was measured, of whom, or why — and "why" was not even in the payload until
// the engine started carrying the judge's reasoning with the number it explains.
//
// Every kind states its own fact here. The raw payload stays reachable (the card
// keeps it behind a disclosure) because an evidence view that hides data is the
// other failure mode; the point is that the reader should not have to decode it
// to learn what happened.

import type { Finding } from "./types";

export interface FindingSummary {
  // The fact, as a sentence. Never empty.
  headline: string;
  // The judge's or rule's own justification, when there is one. Rendered as a
  // quote, not as a key-value row: it is prose and reads as prose.
  reasoning?: string;
  // Short typed chips (a flag name, a rule id) worth surfacing beside the text.
  tags: string[];
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

function score(v: unknown): string {
  const n = num(v);
  return n === null ? "unknown" : n.toFixed(2);
}

function agentOf(f: Finding, fallback: string): string {
  return str(f.data?.agent) ?? fallback;
}

/** The sentence for one finding. `runLabel` resolves a run_id to a node name. */
export function summarizeFinding(
  f: Finding,
  runLabel?: (runId: string) => string,
): FindingSummary {
  const d = (f.data ?? {}) as Record<string, unknown>;
  const fallback =
    f.subject.scope === "run" && f.subject.run_id
      ? (runLabel?.(f.subject.run_id) ?? f.subject.run_id.slice(0, 8))
      : f.subject.scope === "terminal"
        ? "the deliverable"
        : "the graph";
  const who = agentOf(f, fallback);
  const reasoning = str(d.reasoning) ?? str(d.detail) ?? undefined;
  const tags: string[] = [];

  switch (f.kind) {
    case "content_score":
      return { headline: `Quality judge scored ${who} ${score(d.score)}`, reasoning, tags };

    case "content_flag": {
      const flag = str(d.flag);
      if (flag) tags.push(flag);
      return { headline: `Judge flagged ${who}`, reasoning, tags };
    }

    case "content_drop": {
      const base = num(d.base);
      const now = num(d.score);
      const drop = num(d.drop);
      const arrow =
        base !== null && now !== null ? `${base.toFixed(2)} → ${now.toFixed(2)}` : "";
      const by = drop !== null ? ` (−${drop.toFixed(2)})` : "";
      return {
        headline: `Quality fell across ${who}${arrow ? `: ${arrow}` : ""}${by}`,
        reasoning,
        tags,
      };
    }

    case "terminal_content": {
      const bad = d.bad === true;
      return {
        headline: bad
          ? `Terminal judge REJECTED the deliverable (${score(d.score)})`
          : `Terminal judge accepted the deliverable (${score(d.score)})`,
        reasoning,
        tags: d.checkable === false ? ["not checkable"] : tags,
      };
    }

    case "terminal_form": {
      const req = str(d.requirement);
      const seen = str(d.observed);
      return {
        headline:
          d.bad === true
            ? `Deliverable form does not match the request${req ? `: “${req}”` : ""}`
            : "Deliverable form matches the request",
        reasoning: reasoning ?? (seen ? `Observed: ${seen}` : undefined),
        tags,
      };
    }

    case "contract_breach": {
      const key = str(d.key) ?? "a carried parameter";
      const from = str(d.from) ?? str(d.input_value);
      const to = str(d.to) ?? str(d.output_value);
      return {
        headline:
          from && to
            ? `${who} rewrote ${key}: ${from} → ${to}`
            : `${who} silently rewrote ${key}`,
        reasoning,
        tags: [key],
      };
    }

    case "deterministic_signal": {
      const rule = str(d.rule) ?? str(d.name);
      if (rule) tags.push(rule);
      return {
        headline: reasoning ? `${who} — ${reasoning}` : `Rule fired on ${who}`,
        reasoning: str(d.basis) ?? undefined,
        tags,
      };
    }

    case "input_flawed":
      return {
        headline: `${who} reports its OWN input was already flawed`,
        reasoning,
        tags,
      };

    case "verifier_verdict": {
      const issued = str(d.issued) ?? (d.passed === true ? "PASS" : "FAIL");
      return {
        headline: `${who} issued ${issued} and the check disagrees`,
        reasoning,
        tags,
      };
    }

    case "loop_anomaly": {
      const it = num(d.iterations);
      return {
        headline: `Loop ran ${it ?? "?"} iteration(s) past the limit at ${who}`,
        reasoning,
        tags: str(d.limit_kind) ? [String(d.limit_kind)] : tags,
      };
    }

    case "required_section": {
      const section = str(d.section);
      return {
        headline: section
          ? `${who} is missing the required section “${section}”`
          : `${who} is missing required content`,
        reasoning,
        tags,
      };
    }

    case "breach_propagated":
      return {
        headline: "The rewritten value reached the shipped deliverable",
        reasoning,
        tags,
      };

    case "breach_corrected":
      return {
        headline: "The rewritten value was corrected before shipping",
        reasoning,
        tags,
      };

    default: {
      // An unknown kind must still say something true rather than nothing: the
      // catalogue grows whenever a detector is added, and a UI that renders a
      // blank row for a finding it has not met yet hides evidence.
      const kind = f.kind.replace(/_/g, " ");
      return { headline: `${kind} — ${who}`, reasoning, tags };
    }
  }
}

/** Keys already spoken by the headline, so the raw view can skip repeating them. */
export const SPOKEN_KEYS = new Set([
  "agent",
  "score",
  "flag",
  "base",
  "drop",
  "reasoning",
  "detail",
  "bad",
  "value",
  "key",
  "from",
  "to",
  "requirement",
  "observed",
  "iterations",
  "limit_kind",
  "section",
  "rule",
  "name",
]);
