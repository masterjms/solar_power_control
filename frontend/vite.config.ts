import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 개발용 dev server. /api, /health 는 백엔드(8000)로 프록시한다.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
