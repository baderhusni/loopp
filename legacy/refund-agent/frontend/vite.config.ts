import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the frontend runs on :5173 and proxies /api to the FastAPI backend on
// :8000, so the app can always call relative "/api/..." URLs (which also works
// when the backend serves the built frontend from a single origin).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
