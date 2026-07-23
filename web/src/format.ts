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
