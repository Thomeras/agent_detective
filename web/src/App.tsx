// App shell: a persistent left sidebar plus one scrolling main column.
//
// The window itself never scrolls — the sidebar, the page header and the page
// body are fixed regions, and only the body scrolls. That is what makes long
// lists usable: the header and nav stay put no matter how far down you are.

import type { ReactNode } from "react";

import Contracts from "./screens/Contracts";
import GraphList from "./screens/GraphList";
import GraphView from "./screens/GraphView";
import IncidentInbox from "./screens/IncidentInbox";
import Leaderboard from "./screens/Leaderboard";
import { api } from "./api/client";
import { href, useRoute } from "./router";
import { useAsync } from "./hooks/useAsync";
import { useTheme } from "./ui/theme";

type Section = "incidents" | "leaderboard" | "graphs" | "contracts";

function IconIncidents() {
  return (
    <svg className="ico" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M8 1.8l6 10.9H2L8 1.8z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <path d="M8 6v3.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="8" cy="11" r="0.85" fill="currentColor" />
    </svg>
  );
}

function IconRuns() {
  return (
    <svg className="ico" viewBox="0 0 16 16" fill="none" aria-hidden>
      <circle cx="3.6" cy="3.6" r="2.1" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="12.4" cy="8" r="2.1" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="3.6" cy="12.4" r="2.1" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M5.6 4.6l4.9 2.5M5.6 11.4l4.9-2.5"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

function IconAgents() {
  return (
    <svg className="ico" viewBox="0 0 16 16" fill="none" aria-hidden>
      <rect x="2" y="8.5" width="3.2" height="5.5" rx="0.8" stroke="currentColor" strokeWidth="1.4" />
      <rect x="6.4" y="4.5" width="3.2" height="9.5" rx="0.8" stroke="currentColor" strokeWidth="1.4" />
      <rect x="10.8" y="6.5" width="3.2" height="7.5" rx="0.8" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

function IconContracts() {
  return (
    <svg className="ico" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M4 2h5l3 3v9H4V2z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M9 2v3h3" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M6 9h4M6 11.5h4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

function NavLink({
  to,
  label,
  icon,
  active,
  badge,
  alert,
}: {
  to: string;
  label: string;
  icon: ReactNode;
  active: boolean;
  badge?: number;
  alert?: boolean;
}) {
  return (
    <a className={`side-link${active ? " active" : ""}`} href={href(to)}>
      {icon}
      <span>{label}</span>
      {badge !== undefined && badge > 0 && (
        <span className={`side-badge${alert ? " alert" : ""}`}>{badge}</span>
      )}
    </a>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      className="icon-btn"
      title={`Switch to ${next} theme`}
      aria-label={`Switch to ${next} theme`}
      onClick={() => setTheme(next)}
    >
      {theme === "dark" ? (
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden>
          <path
            d="M13.2 9.6A5.6 5.6 0 016.4 2.8 5.6 5.6 0 108 13.6c2.3 0 4.3-1.6 5.2-4z"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinejoin="round"
          />
        </svg>
      ) : (
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden>
          <circle cx="8" cy="8" r="3" stroke="currentColor" strokeWidth="1.4" />
          <path
            d="M8 1v1.6M8 13.4V15M15 8h-1.6M2.6 8H1M12.9 3.1l-1.1 1.1M4.2 11.8l-1.1 1.1M12.9 12.9l-1.1-1.1M4.2 4.2L3.1 3.1"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
          />
        </svg>
      )}
    </button>
  );
}

// A dead API is otherwise indistinguishable from an empty database — both
// render as "nothing here". Reuses the nav-badge fetch rather than probing again.
function ApiStatus({ up, error }: { up: boolean; error: string | null }) {
  return (
    <>
      <span className={`api-dot${up ? "" : " down"}`} aria-hidden />
      <span className="side-foot-text" title={error ?? undefined}>
        {up ? "API connected" : error ? "API unreachable" : "connecting…"}
      </span>
    </>
  );
}

export default function App() {
  const { path, query } = useRoute();

  // One shared read for the nav badge: how many incidents still need a human.
  const inbox = useAsync(() => api.listIncidents(200), []);
  const openCount = (inbox.data?.incidents ?? []).filter(
    (i) => i.status === "open" || i.status === "acknowledged",
  ).length;

  let screen: ReactNode;
  let section: Section;

  const graphMatch = path.match(/^\/graphs\/([^/]+)$/);
  if (graphMatch) {
    const incidentParam = query.get("incident");
    const incidentId = incidentParam ? Number(incidentParam) : null;
    screen = (
      <GraphView
        graphId={decodeURIComponent(graphMatch[1])}
        incidentId={incidentId !== null && Number.isFinite(incidentId) ? incidentId : null}
      />
    );
    section = "graphs";
  } else if (path === "/graphs") {
    screen = <GraphList />;
    section = "graphs";
  } else if (path === "/leaderboard") {
    screen = <Leaderboard />;
    section = "leaderboard";
  } else if (path === "/contracts") {
    screen = <Contracts />;
    section = "contracts";
  } else {
    screen = <IncidentInbox />;
    section = "incidents";
  }

  return (
    <div className="app">
      <nav className="sidebar">
        <a className="sidebar-brand" href={href("/incidents")}>
          <span className="brand-mark">AD</span>
          <span className="brand-text">
            <span className="brand-name">Agent Detective</span>
            <span className="brand-tag">blame engine</span>
          </span>
        </a>

        <div className="side-nav">
          <div className="side-group-label">Investigate</div>
          <NavLink
            to="/incidents"
            label="Incidents"
            icon={<IconIncidents />}
            active={section === "incidents"}
            badge={openCount}
            alert
          />
          <NavLink to="/graphs" label="Runs" icon={<IconRuns />} active={section === "graphs"} />
          <div className="side-group-label">Analyse</div>
          <NavLink
            to="/leaderboard"
            label="Agents"
            icon={<IconAgents />}
            active={section === "leaderboard"}
          />
          <NavLink
            to="/contracts"
            label="Contracts"
            icon={<IconContracts />}
            active={section === "contracts"}
          />
        </div>

        <div className="side-foot">
          <ApiStatus up={Boolean(inbox.data) && !inbox.error} error={inbox.error} />
          <ThemeToggle />
        </div>
      </nav>

      <main className="main">{screen}</main>
    </div>
  );
}
