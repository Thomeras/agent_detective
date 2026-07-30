// Display helpers shared across screens.

export function shortId(id: string | null | undefined): string {
  if (!id) return "-";
  return id.length > 8 ? id.slice(0, 8) : id;
}

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `$${value.toFixed(4)}`;
}

export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  if (value === 0) return "$0";
  // LLM runs cost fractions of a cent: two decimals rendered real spend as
  // "$0.00" while the per-node panel showed the money. Scale the precision to
  // the magnitude instead of flooring small amounts away.
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 1) return `$${value.toFixed(3)}`;
  return `$${value.toFixed(2)}`;
}

export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined) return "unknown";
  return value.toFixed(2);
}

// The full quality-score channel set (services/worker scoring weights). Coverage
// is counted against THIS set, so a blend fed by one channel can never present
// itself as a three-channel composite.
export const SCORE_CHANNELS = ["schema", "judge", "heuristics"] as const;

export interface ChannelCoverage {
  reported: number;
  total: number;
}

export function channelCoverage(
  components: Record<string, number | null> | null | undefined,
): ChannelCoverage {
  // An unrecognised channel widens the denominator rather than being dropped.
  const channels = new Set<string>(SCORE_CHANNELS);
  Object.keys(components ?? {}).forEach((key) => channels.add(key));
  const reported = Object.values(components ?? {}).filter(
    (v) => v !== null && v !== undefined,
  ).length;
  return { reported, total: channels.size };
}

// The EFFECTIVE weights (post-renormalization) as recorded. Never derived from
// nominal weights — this client does not know them, and guessing would invent
// provenance.
export function formatWeights(
  weights: Record<string, number> | null | undefined,
): string | null {
  if (!weights) return null;
  const entries = Object.entries(weights).filter(([, w]) => Number.isFinite(w));
  if (entries.length === 0) return null;
  return entries
    .sort((a, b) => b[1] - a[1])
    .map(([channel, w]) => `${channel} ${Math.round(w * 100)}%`)
    .join(" · ");
}

// A cost names the coverage it was summed over: anything short of full coverage
// is a lower bound, because an unpriced run is unknown spend, not free.
export function formatCoverage(
  coverage: { priced: number; total: number } | null | undefined,
): string | null {
  if (!coverage || !Number.isFinite(coverage.total) || coverage.total <= 0) return null;
  const bound = coverage.priced < coverage.total ? " · lower bound" : "";
  return `${coverage.priced}/${coverage.total} runs${bound}`;
}

// Sum a set of rows whose costs may be unmeasured. Returns the total AND what
// it covers, because `?? 0` on an unpriced row silently asserts it was free.
export function sumWithCoverage(
  rows: { cost: number | null | undefined; priced?: number | null; total?: number | null }[],
): { total: number | null; coverage: { priced: number; total: number } } {
  let sum = 0;
  let seen = false;
  let priced = 0;
  let runs = 0;
  for (const row of rows) {
    if (typeof row.cost === "number" && Number.isFinite(row.cost)) {
      sum += row.cost;
      seen = true;
    }
    // Row-level run coverage when the API knows it; otherwise the row itself
    // is the unit and it counts as priced only if it carried a cost.
    if (typeof row.total === "number" && row.total > 0) {
      runs += row.total;
      priced += typeof row.priced === "number" ? row.priced : 0;
    } else {
      runs += 1;
      priced += typeof row.cost === "number" ? 1 : 0;
    }
  }
  return { total: seen ? sum : null, coverage: { priced, total: runs } };
}

// One wording for a missing instrument, everywhere — never imply a model.
export function judgeLabel(model: string | null | undefined): string {
  return model && model.trim() ? model.trim() : "judge not recorded";
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

export function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `${(value * 100).toFixed(0)}%`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const diffMs = Date.now() - date.getTime();
  const sec = Math.round(diffMs / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}

// Maps a quality score in [0,1] to a green->red gradient; null renders gray.
export function scoreColor(score: number | null | undefined): string {
  if (score === null || score === undefined) return "#6b7280"; // gray = unknown
  const s = Math.max(0, Math.min(1, score));
  // 0 -> red (hue 0), 1 -> green (hue 130)
  const hue = s * 130;
  return `hsl(${hue}, 65%, 45%)`;
}
