// Theme selection, held in a module-level store so every consumer sees the same
// value. The canvas has to rebuild its cytoscape stylesheet when the theme
// flips, and it can only do that if it observes the same signal as the toggle.
//
// The attribute is written to <html> before React mounts (see main.tsx) so the
// first paint already carries the right palette.

import { useSyncExternalStore } from "react";

export type Theme = "dark" | "light";

const KEY = "ad.theme";
const listeners = new Set<() => void>();
let current: Theme = "dark";

function systemTheme(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function initialTheme(): Theme {
  let stored: string | null = null;
  try {
    stored = window.localStorage?.getItem(KEY);
  } catch {
    // Private-mode storage denial: fall back to the OS preference.
  }
  return stored === "light" || stored === "dark" ? stored : systemTheme();
}

export function applyTheme(theme: Theme): void {
  current = theme;
  document.documentElement.setAttribute("data-theme", theme);
}

export function setTheme(next: Theme): void {
  applyTheme(next);
  try {
    window.localStorage?.setItem(KEY, next);
  } catch {
    // Persistence is best-effort; the toggle must still work without it.
  }
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useTheme(): [Theme, (next: Theme) => void] {
  const theme = useSyncExternalStore(
    subscribe,
    () => current,
    () => current,
  );
  return [theme, setTheme];
}
