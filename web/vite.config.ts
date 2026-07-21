import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on 5173 per the build spec. The API base URL is read at build
// time from VITE_API_BASE_URL (see src/api/client.ts).
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
  },
  preview: {
    host: true,
    port: 5173,
  },
});
