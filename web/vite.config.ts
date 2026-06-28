import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the React app runs on :5173 and the backend on :8000. The
// backend's session cookie is samesite=lax + path=/, so we proxy every
// API path to keep the browser on a single origin. In production both
// land behind Caddy on the same domain.
const proxyTarget = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/auth": proxyTarget,
      "/channels": proxyTarget,
      "/workspace": proxyTarget,
      "/tasks": proxyTarget,
      "/schedules": proxyTarget,
      "/usage": proxyTarget,
      "/me": proxyTarget,
      "/health": proxyTarget,
    },
  },
});
