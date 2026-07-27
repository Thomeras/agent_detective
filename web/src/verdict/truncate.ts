// Word-boundary truncation (verdict-refactor-plan.md §11 row 16).
//
// Kills the "unto[uched]" / "…assumed" class of bugs: prose clipped mid-word
// destroys auditability by eating the operative token. This helper NEVER clips
// inside a word — it backs up to the previous word boundary. Structured caveats
// are FIELDS on a Defect (rendered as chips) and must NOT be passed through
// here at all; this is for gist/headline prose only (§2.4 render rules).
//
// Pure and dependency-free — trivially unit-testable (see truncate.test.ts).

export interface TruncateOptions {
  // Appended when (and only when) the text is actually clipped. Default "…".
  ellipsis?: string;
  // If the first word alone already exceeds maxLength, allow a hard mid-word
  // cut as a last resort rather than returning an empty string. Default false
  // (prefer returning the whole first word untouched — never a fragment).
  hardBreakLongWord?: boolean;
}

// Clip `text` so the RESULT (including the ellipsis) is at most `maxLength`
// characters, breaking only at whitespace. Returns the input unchanged when it
// already fits or when maxLength is non-positive-guarded nonsense.
export function truncateWords(
  text: string,
  maxLength: number,
  options: TruncateOptions = {},
): string {
  const { ellipsis = "…", hardBreakLongWord = false } = options;

  if (typeof text !== "string" || text.length === 0) return "";
  if (!Number.isFinite(maxLength) || maxLength <= 0) return "";
  // Already fits: nothing to clip, no ellipsis.
  if (text.length <= maxLength) return text;

  // Budget for actual content once the ellipsis is reserved.
  const budget = maxLength - ellipsis.length;
  // Ellipsis alone doesn't fit: degrade to a bare (possibly hard) clip.
  if (budget <= 0) {
    return hardBreakLongWord ? text.slice(0, maxLength) : "";
  }

  // Take the candidate window, then retreat to the last whitespace inside it so
  // we never end in the middle of a word.
  const window = text.slice(0, budget);
  const lastSpace = lastWhitespaceIndex(window);

  let head: string;
  if (lastSpace > 0) {
    head = window.slice(0, lastSpace);
  } else if (hardBreakLongWord) {
    // No boundary at all and the first word overruns: hard-cut as last resort.
    head = window;
  } else {
    // No boundary and hard breaking disabled: keep the whole first word rather
    // than emit a fragment — clipping mid-word is exactly the bug we forbid.
    const firstBreak = firstWhitespaceIndex(text);
    head = firstBreak > 0 ? text.slice(0, firstBreak) : text;
  }

  // Drop any trailing punctuation/space left dangling before the ellipsis.
  head = head.replace(/[\s.,;:!?–—-]+$/u, "");
  if (head.length === 0) {
    return hardBreakLongWord ? text.slice(0, budget) + ellipsis : "";
  }
  return head + ellipsis;
}

function lastWhitespaceIndex(s: string): number {
  for (let i = s.length - 1; i >= 0; i--) {
    if (isWhitespace(s[i])) return i;
  }
  return -1;
}

function firstWhitespaceIndex(s: string): number {
  for (let i = 0; i < s.length; i++) {
    if (isWhitespace(s[i])) return i;
  }
  return -1;
}

function isWhitespace(ch: string): boolean {
  return ch === " " || ch === "\t" || ch === "\n" || ch === "\r" || ch === "\f" || ch === "\v";
}
