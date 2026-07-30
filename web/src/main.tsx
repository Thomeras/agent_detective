import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { applyTheme, initialTheme } from "./ui/theme";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root container #root not found");
}

// Before first paint, so the shell never flashes the wrong palette.
applyTheme(initialTheme());

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
