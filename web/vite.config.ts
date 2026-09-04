import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the Control API so the frontend and backend
// run independently during development. Build output goes to web/dist/ and
// is served by the Control API's StaticFiles mount in production.
const apiPort = process.env.CLICKCLICK_API_PORT ?? "8080";
// When the Control API runs with TLS (CLICKCLICK_API_SSL_CERTFILE), plain
// http:// requests are 308-redirected, which the dev proxy cannot follow —
// proxy straight to https and accept the self-signed dev cert instead.
const apiTls = Boolean(process.env.CLICKCLICK_API_SSL_CERTFILE);
const apiTarget = `${apiTls ? "https" : "http"}://127.0.0.1:${apiPort}`;

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        secure: !apiTls,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
