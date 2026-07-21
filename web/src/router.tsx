// Minimal dependency-free hash router. Routes are encoded in location.hash so
// the app works when served as static files behind nginx (no SPA rewrite for
// deep links needed). Supported routes:
//   #/                -> incidents inbox
//   #/incidents       -> incidents inbox
//   #/graphs/:id      -> graph view (optionally ?incident=<id>)
//   #/leaderboard     -> agent leaderboard

import { useEffect, useState } from "react";

export interface Route {
  path: string; // e.g. "/graphs/abc"
  query: URLSearchParams;
}

function parseHash(): Route {
  const raw = window.location.hash.replace(/^#/, "");
  const [path, queryString = ""] = raw.split("?");
  return {
    path: path === "" ? "/" : path,
    query: new URLSearchParams(queryString),
  };
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(parseHash);
  useEffect(() => {
    const onChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function navigate(path: string): void {
  window.location.hash = path;
}

export function href(path: string): string {
  return `#${path}`;
}
