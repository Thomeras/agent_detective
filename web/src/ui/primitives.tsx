// Typed primitive components for the verdict rebuild (Track B, §9.3).
//
// Six small, tokenised primitives — Badge, Card, Meter, Table, Disclosure,
// Chip — built on the design tokens in tokens.css. No external component
// library (CSP constraint). Each maps a `tone` / `channel` prop onto the
// `.ad-tone-*` / `.ad-channel-*` helper classes so colour stays in one place.

import type { ReactNode } from "react";

import "./tokens.css";
import type { Tone } from "../verdict/descriptor";
import type { Channel } from "../verdict/types";

// Map a semantic tone or channel to its CSS helper class (sets --ad-tone-*).
function toneClass(tone?: Tone): string {
  return tone ? `ad-tone-${tone}` : "";
}
function channelClass(channel?: Channel): string {
  return channel ? `ad-channel-${channel}` : "";
}

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// ---------------------------------------------------------------------------
// Badge — a compact status/label pill. Colour by `tone` OR `channel`.
// ---------------------------------------------------------------------------
export function Badge({
  children,
  tone,
  channel,
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  channel?: Channel;
  title?: string;
}) {
  return (
    <span className={cx("ad-badge", toneClass(tone), channelClass(channel))} title={title}>
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Chip — a small labelled pill for caveat fields (§2.4). Wraps, never clips.
// ---------------------------------------------------------------------------
export function Chip({
  children,
  tone,
  channel,
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  channel?: Channel;
  title?: string;
}) {
  return (
    <span className={cx("ad-chip", toneClass(tone), channelClass(channel))} title={title}>
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Card — the primary container (defect cards, panels).
// ---------------------------------------------------------------------------
export function Card({
  children,
  title,
  tone,
  channel,
  actions,
}: {
  children: ReactNode;
  title?: ReactNode;
  tone?: Tone;
  channel?: Channel;
  // Right-aligned content in the header (badges, meters).
  actions?: ReactNode;
}) {
  const toned = Boolean(tone || channel);
  return (
    <section className={cx("ad-card", toned && "ad-toned", toneClass(tone), channelClass(channel))}>
      {(title || actions) && (
        <div className="ad-card-head">
          {title && <h3 className="ad-card-title">{title}</h3>}
          {actions}
        </div>
      )}
      <div className="ad-card-body">{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Meter — renders the observation/attribution confidence PAIR (§2.4).
// Both are always shown; attribution === null renders as "n/a", never as 0,
// so the two claims can never read as one number.
// ---------------------------------------------------------------------------
function pct(value: number): string {
  return `${Math.round(clamp01(value) * 100)}%`;
}
function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

function MeterRow({ label, value, tone }: { label: string; value: number | null; tone?: Tone }) {
  const has = value != null && Number.isFinite(value);
  return (
    <div className="ad-meter-row">
      <span className="ad-meter-label">{label}</span>
      <span className="ad-meter-track">
        {has && (
          <span className={cx("ad-meter-fill", toneClass(tone))} style={{ width: pct(value) }} />
        )}
      </span>
      <span className={cx("ad-meter-value", !has && "ad-na")}>{has ? pct(value) : "n/a"}</span>
    </div>
  );
}

export function Meter({
  observation,
  attribution,
  tone,
}: {
  // Is the output defective?
  observation: number;
  // Did it originate here? null when the defect is unattributed.
  attribution: number | null;
  tone?: Tone;
}) {
  return (
    <div className="ad-meter">
      <MeterRow label="Observed" value={observation} tone={tone} />
      <MeterRow label="Attributed" value={attribution} tone={tone} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Table — a typed, generic data table.
// ---------------------------------------------------------------------------
export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
  // Right-align + tabular numerals for numeric columns.
  numeric?: boolean;
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
}) {
  return (
    <div className="ad-table-wrap">
      <table className="ad-table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.numeric ? "ad-col-num" : undefined}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              className={onRowClick ? "ad-row-link" : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((c) => (
                <td key={c.key} className={c.numeric ? "ad-col-num" : undefined}>
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Disclosure — a native <details> wrapper (evidence behind a summary).
// ---------------------------------------------------------------------------
export function Disclosure({
  summary,
  children,
  defaultOpen = false,
}: {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  return (
    <details className="ad-disclosure" open={defaultOpen}>
      <summary className="ad-disclosure-summary">{summary}</summary>
      <div className="ad-disclosure-body">{children}</div>
    </details>
  );
}
