// App shell: top navigation plus hash-router dispatch to the three screens.

import GraphView from "./screens/GraphView";
import IncidentInbox from "./screens/IncidentInbox";
import Leaderboard from "./screens/Leaderboard";
import { href, useRoute } from "./router";

function NavLink({ to, label, active }: { to: string; label: string; active: boolean }) {
  return (
    <a className={`nav-link${active ? " active" : ""}`} href={href(to)}>
      {label}
    </a>
  );
}

export default function App() {
  const route = useRoute();
  const { path, query } = route;

  let screen: JSX.Element;
  let section: "incidents" | "leaderboard" | "graphs";

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
  } else if (path === "/leaderboard") {
    screen = <Leaderboard />;
    section = "leaderboard";
  } else {
    screen = <IncidentInbox />;
    section = "incidents";
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">AD</span>
          <span className="brand-name">Agent Detective</span>
        </div>
        <nav className="nav">
          <NavLink to="/incidents" label="Incidents" active={section === "incidents"} />
          <NavLink to="/leaderboard" label="Leaderboard" active={section === "leaderboard"} />
        </nav>
      </header>
      <main className="content">{screen}</main>
    </div>
  );
}
