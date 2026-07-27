// Unit tests for truncateWords (verdict-refactor-plan.md §11 row 16).
//
// Framework-free on purpose: the web package has no test runner and no
// @types/node, so this uses a tiny inline assert and self-executes. Run with a
// TypeScript-aware Node (>= 23.6, native type stripping):
//
//     node src/verdict/truncate.test.ts
//
// It exits non-zero (throws) on the first failure. It is not imported anywhere
// in the app, so it never ships in the bundle.

import { truncateWords } from "./truncate.ts";

let checks = 0;

function eq(actual: string, expected: string, label: string): void {
  checks++;
  if (actual !== expected) {
    throw new Error(`FAIL ${label}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`);
  }
}

function ok(cond: boolean, label: string): void {
  checks++;
  if (!cond) throw new Error(`FAIL ${label}`);
}

// Fits already: returned unchanged, no ellipsis.
eq(truncateWords("short text", 20), "short text", "fits unchanged");
eq(truncateWords("exactfit", 8), "exactfit", "exact length unchanged");

// The canonical bug: never clip mid-word. "untouched" must not become "unto…".
{
  const out = truncateWords("the file was left untouched", 22);
  ok(!/unto$/.test(out.replace("…", "")), "never emits the 'unto' fragment");
  ok(out.endsWith("…"), "clipped result carries an ellipsis");
  ok(out.length <= 22, "result within budget");
  // Boundary retreat lands on a whole word.
  eq(out, "the file was left…", "retreats to whole-word boundary");
}

// The "…assumed" admission must survive as a whole word when it fits the window.
{
  const out = truncateWords("baseline is assumed not measured here", 24);
  ok(out.endsWith("…"), "clipped");
  ok(!out.replace("…", "").endsWith("assum"), "no 'assum' fragment");
}

// Trailing punctuation before the ellipsis is dropped (no "word,…").
eq(truncateWords("alpha, beta, gamma delta", 14), "alpha, beta…", "drops dangling comma");

// A single word longer than the budget: keep it whole rather than fragment.
{
  const out = truncateWords("supercalifragilistic tail", 10);
  eq(out, "supercalifragilistic…", "keeps overlong first word whole by default");
}

// Hard-break opt-in: a fragment is allowed only when explicitly requested.
{
  const out = truncateWords("supercalifragilistic", 10, { hardBreakLongWord: true });
  ok(out.length <= 10, "hard break respects budget");
  ok(out.endsWith("…"), "hard break still ellipsises");
}

// Custom ellipsis is honoured and counted against the budget.
{
  const out = truncateWords("one two three four five", 12, { ellipsis: "..." });
  ok(out.endsWith("..."), "custom ellipsis applied");
  ok(out.length <= 12, "custom ellipsis within budget");
}

// Degenerate inputs never throw and never fabricate content.
eq(truncateWords("", 10), "", "empty in, empty out");
eq(truncateWords("anything", 0), "", "non-positive budget is empty");
eq(truncateWords("anything", -5), "", "negative budget is empty");

// Newlines and tabs count as boundaries too.
eq(truncateWords("line one\nline two here", 12), "line one…", "newline is a boundary");

console.log(`truncateWords: ${checks} checks passed`);
