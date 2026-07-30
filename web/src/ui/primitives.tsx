// Typed primitive components. Presentation lives here and in styles.css; no
// external component library (CSP constraint).
//
// Each primitive maps a semantic `tone` / `channel` onto the `.ad-tone-*` /
// `.ad-channel-*` helper classes, so colour is decided in exactly one place.
//
// The list primitives (Record*, Toolbar, Segmented, StatTile) exist because the
// data this app shows does not fit a fixed-column table: ids, agent names and
// judge prose all vary in width, and a table answers that by clipping. A record
// row labels every field and wraps instead.

import type { ChangeEvent, ReactNode } from "react";

import type { Tone } from "../verdict/descriptor";
import type { Channel } from "../verdict/types";

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
// Badge / Chip
// ---------------------------------------------------------------------------

export function Badge({
  children,
  tone,
  channel,
  title,
  size,
}: {
  children: ReactNode;
  tone?: Tone;
  channel?: Channel;
  title?: string;
  size?: "lg";
}) {
  return (
    <span
      className={cx("ad-badge", size === "lg" && "lg", toneClass(tone), channelClass(channel))}
      title={title}
    >
      {children}
    </span>
  );
}

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
// Card
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
// Meter — the observation/attribution confidence PAIR (§2.4).
// Both are always shown; attribution === null renders "n/a", never 0, so the
// two claims can never read as one number.
// ---------------------------------------------------------------------------

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}
function pct(value: number): string {
  return `${Math.round(clamp01(value) * 100)}%`;
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
  observation: number;
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

// A bare proportion bar for list rows (quality, failure rate).
export function Bar({ value, tone }: { value: number | null; tone?: Tone }) {
  const has = value != null && Number.isFinite(value);
  return (
    <span className={cx("bar", toneClass(tone))}>
      {has && <span style={{ width: pct(value) }} />}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Table — kept for genuinely tabular data. Sticky header, its own scroll box,
// and wrapping cells, so nothing is pushed out of view.
// ---------------------------------------------------------------------------

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
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
// Disclosure
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

// ---------------------------------------------------------------------------
// Record list — a row per entity, every field labelled and free to wrap.
// ---------------------------------------------------------------------------

export function RecordList({ children }: { children: ReactNode }) {
  return <div className="rec-list">{children}</div>;
}

export function RecordRow({
  tone,
  onClick,
  selected,
  children,
  href,
  dense,
}: {
  tone?: Tone;
  onClick?: () => void;
  selected?: boolean;
  children: ReactNode;
  href?: string;
  // Identity and fields share one line while they fit, and wrap when they
  // don't — density for long lists without ever clipping a value.
  dense?: boolean;
}) {
  const className = cx(
    "rec",
    toneClass(tone),
    dense && "dense",
    (onClick || href) && "clickable",
    selected && "selected",
  );
  const body = (
    <>
      <div className="rec-rail" aria-hidden />
      <div className="rec-body">{children}</div>
    </>
  );
  if (href) {
    return (
      <a className={className} href={href}>
        {body}
      </a>
    );
  }
  if (onClick) {
    // A div, not a <button>: the row holds block content, which button's
    // phrasing-only content model forbids.
    return (
      <div
        className={className}
        role="button"
        tabIndex={0}
        onClick={onClick}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onClick();
          }
        }}
      >
        {body}
      </div>
    );
  }
  return <div className={className}>{body}</div>;
}

export function RecordFields({ children }: { children: ReactNode }) {
  return <div className="rec-fields">{children}</div>;
}

export function Field({
  label,
  children,
  tone,
  title,
  wide,
  faint,
}: {
  label: string;
  children: ReactNode;
  tone?: Tone;
  title?: string;
  wide?: boolean;
  faint?: boolean;
}) {
  return (
    <div className={cx("rec-field", wide && "rec-field-wide", toneClass(tone))} title={title}>
      <span className="rec-field-key">{label}</span>
      <span className={cx("rec-field-val", tone && "tone", faint && "faint")}>{children}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page — the per-screen header + the one scrolling region beneath it. Screens
// never scroll the window; they fill this box, which owns its own scrollbar.
// ---------------------------------------------------------------------------

export function Page({
  title,
  subtitle,
  actions,
  back,
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  back?: ReactNode;
  children: ReactNode;
}) {
  return (
    <>
      <header className="page-head">
        <div className="page-head-main">
          {back}
          <h1 className="page-title">{title}</h1>
          {subtitle && <div className="page-sub">{subtitle}</div>}
        </div>
        {actions && <div className="head-actions">{actions}</div>}
      </header>
      <div className="page-body">
        <div className="page-inner">{children}</div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Toolbar controls
// ---------------------------------------------------------------------------

export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="toolbar">{children}</div>;
}

export function SearchInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="search">
      <svg className="ico" viewBox="0 0 16 16" fill="none" aria-hidden>
        <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.5" />
        <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
      <input
        type="search"
        value={value}
        placeholder={placeholder ?? "Search"}
        onChange={(e: ChangeEvent<HTMLInputElement>) => onChange(e.target.value)}
      />
    </label>
  );
}

export interface SegmentOption<V extends string> {
  value: V;
  label: string;
  count?: number;
  title?: string;
}

export function Segmented<V extends string>({
  options,
  value,
  onChange,
}: {
  options: SegmentOption<V>[];
  value: V;
  onChange: (next: V) => void;
}) {
  return (
    <div className="segmented" role="group">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.title}
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
          {o.count !== undefined && <span className="seg-count">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Select<V extends string>({
  value,
  onChange,
  options,
  title,
}: {
  value: V;
  onChange: (next: V) => void;
  options: { value: V; label: string }[];
  title?: string;
}) {
  return (
    <select
      className="select"
      value={value}
      title={title}
      onChange={(e) => onChange(e.target.value as V)}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// Stat tile
// ---------------------------------------------------------------------------

export function StatTile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
}) {
  return (
    <div className={cx("stat", toneClass(tone))}>
      <div className="stat-key">{label}</div>
      <div className="stat-val">{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

export interface TabSpec<V extends string> {
  value: V;
  label: string;
  count?: number;
}

export function Tabs<V extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: TabSpec<V>[];
  value: V;
  onChange: (next: V) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.value}
          type="button"
          role="tab"
          className="tab"
          aria-selected={t.value === value}
          onClick={() => onChange(t.value)}
        >
          {t.label}
          {t.count !== undefined && <span className="tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Drawer — the node inspector. A slide-over instead of a squeezed sidebar, so
// payloads get real width to wrap into.
// ---------------------------------------------------------------------------

export function Drawer({
  title,
  onClose,
  children,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <>
      <button className="drawer-backdrop" aria-label="Close panel" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="false">
        <div className="drawer-head">
          <span className="drawer-title">{title}</span>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden>
              <path
                d="M3 3l8 8M11 3l-8 8"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}
