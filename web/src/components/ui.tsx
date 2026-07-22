// Reusable presentational primitives: state placeholders and small badges.

import type { ReactNode } from "react";

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state state-loading">
      <span className="spinner" aria-hidden />
      <span>{label}...</span>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state state-error">
      <div className="state-title">Something went wrong</div>
      <div className="state-detail">{message}</div>
      {onRetry && (
        <button className="btn" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="state state-empty">
      <div className="state-title">{title}</div>
      {hint && <div className="state-detail">{hint}</div>}
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-status-${status}`}>{status}</span>;
}

export function TypeBadge({ label, kind }: { label: string; kind?: string }) {
  return <span className={`badge badge-type${kind ? ` badge-type-${kind}` : ""}`}>{label}</span>;
}

export function Panel({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      <h3 className="panel-title">{title}</h3>
      <div className="panel-body">{children}</div>
    </section>
  );
}
