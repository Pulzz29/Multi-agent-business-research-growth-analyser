import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server forwards API calls to the backend. Default: port 8000, the same
// default as scripts/run_backend.py. Override with INSIGHTIQ_API_PORT when the
// backend runs on another port.
const backend = `http://127.0.0.1:${process.env.INSIGHTIQ_API_PORT || "8000"}`;

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: backend,
        changeOrigin: true,
      },
      "/health": {
        target: backend,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
