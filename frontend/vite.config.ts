import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: the browser talks to Vite (5173); /api (REST and WebSocket) is proxied to the FastAPI backend, so there is no CORS and no hard-coded host.
// Production: `npm run build` -> dist/, which the backend serves itself (see docs/DEMO.md).
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": { target: backend, ws: true, changeOrigin: true } } },
});
